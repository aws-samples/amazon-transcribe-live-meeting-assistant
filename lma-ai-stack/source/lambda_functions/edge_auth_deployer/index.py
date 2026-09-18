"""
Custom Resource Lambda to deploy Lambda@Edge function in us-east-1.
This function creates, updates, and deletes the Lambda@Edge viewer-request
function that authorizes /vnc/* requests on the CloudFront distribution.

The edge function's source is held here as a string literal and zipped in
memory, because Lambda@Edge supports neither layers nor environment variables:
its configuration (the signing secret's ARN and home region) is substituted into
the code at stack create/update time, and it must run on the Python runtime's
own modules alone.
"""

import io
import json
import urllib.request
import zipfile

import boto3
from botocore.exceptions import ClientError, WaiterError

# cfnresponse module for Python 3.12+
# Based on https://github.com/aws-cloudformation/custom-resource-helper-python
SUCCESS = "SUCCESS"
FAILED = "FAILED"


def send(
    event, context, responseStatus, responseData, physicalResourceId=None, noEcho=False, reason=None
):
    """Send response to CloudFormation"""
    responseUrl = event["ResponseURL"]

    responseBody = {
        "Status": responseStatus,
        "Reason": reason or f"See the details in CloudWatch Log Stream: {context.log_stream_name}",
        "PhysicalResourceId": physicalResourceId or context.log_stream_name,
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "NoEcho": noEcho,
        "Data": responseData,
    }

    json_responseBody = json.dumps(responseBody)

    headers = {"content-type": "", "content-length": str(len(json_responseBody))}

    try:
        # nosec B310 - URL is CloudFormation-provided event["ResponseURL"], always HTTPS, AWS-controlled (not attacker-influenced)
        req = urllib.request.Request(  # nosec B310
            responseUrl, data=json_responseBody.encode("utf-8"), headers=headers, method="PUT"
        )
        # URL is CloudFormation-provided event["ResponseURL"], always HTTPS, AWS-controlled.
        with urllib.request.urlopen(req) as response:  # nosec B310 # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            print(f"Status code: {response.status}")
    except Exception as e:
        print(f"send(..) failed executing request: {e}")


# Lambda@Edge function code.
#
# Substituted and zipped by this deployer at stack create/update time. Uses only
# the Python runtime's own modules: Lambda@Edge cannot attach layers.
EDGE_FUNCTION_CODE = '''
import base64
import hashlib
import hmac
import json
import time
import urllib.parse

import boto3

# Injected by the deployer. Lambda@Edge has no environment variables, so the
# configuration is baked into the code.
SIGNING_SECRET_ARN = "SIGNING_SECRET_ARN_PLACEHOLDER"
SIGNING_SECRET_REGION = "SIGNING_SECRET_REGION_PLACEHOLDER"

# Upper bound on how far ahead a token's expiry may sit. The minter issues a few
# minutes; this ceiling is checked here as well so the edge does not depend on the
# minter having done so, and it is what rejects an expiry of Infinity.
MAX_TOKEN_LIFETIME_SECONDS = 3600

# One cached copy per warm container. The secret is generated once per
# deployment, so there is nothing to refresh.
_signing_secret = []


def get_signing_secret():
    """The deployment's signing secret, or None when it cannot be read."""
    if _signing_secret:
        return _signing_secret[0]
    try:
        # Lambda@Edge runs replicated in the edge location nearest the viewer, so
        # the region holding the secret has to be named explicitly rather than
        # inherited from the execution environment.
        client = boto3.client("secretsmanager", region_name=SIGNING_SECRET_REGION)
        secret = client.get_secret_value(SecretId=SIGNING_SECRET_ARN)["SecretString"]
    except Exception as exc:
        print("Could not read the signing secret: %s" % type(exc).__name__)
        return None
    _signing_secret.append(secret)
    return secret


def b64url_decode(value):
    """Decode unpadded base64url."""
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def vp_id_from_uri(uri):
    """The participant id in /vnc/<vpId>, or "" when the shape is unexpected."""
    parts = uri.split("/")
    if len(parts) < 3 or parts[1] != "vnc":
        return ""
    return parts[2]


def token_from_querystring(querystring):
    """The value of the `token` query parameter, or None."""
    for param in (querystring or "").split("&"):
        if "=" not in param:
            continue
        key, value = param.split("=", 1)
        if key == "token":
            return urllib.parse.unquote(value)
    return None


def uri_is_plain(uri):
    """True when the path is already in the form the token can be compared to.

    CloudFront normalizes the path to pick a cache behavior but forwards the path
    as the viewer sent it, so the string compared here is the one the origin will
    receive. Paths carrying a relative segment, an encoded dot, or an empty
    segment are refused outright rather than normalized: normalizing correctly is
    the hard part, and this function's only job is to establish that the single
    comparison below is the whole of the path check.
    """
    lowered = uri.lower()
    return ".." not in lowered and "%2e" not in lowered and "//" not in lowered


def verify_token(token, secret, vp_id, uri):
    """True when the token is intact, unexpired, and issued for this exact path.

    The MAC covers the encoded payload exactly as received, so the payload is
    parsed only once the signature has been confirmed. The payload names one
    participant and one path, and both are compared for equality, so a token is
    usable on the single path it was issued for and no other.
    """
    parts = token.split(".")
    if len(parts) != 2:
        return False
    payload_b64, signature_b64 = parts
    try:
        provided = b64url_decode(signature_b64)
    except Exception:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(expected, provided):
        return False
    try:
        payload = json.loads(b64url_decode(payload_b64))
    except Exception:
        return False
    # Every read below assumes a mapping. A signed scalar would otherwise raise
    # out of this function rather than returning a decision.
    if not isinstance(payload, dict):
        return False
    try:
        expiry = float(payload.get("exp", 0))
    except (TypeError, ValueError):
        return False
    # Bounded on both sides, as a positive test, so the only values that pass are
    # the ones that genuinely sit in the window. json accepts the bare literals
    # NaN and Infinity and float() converts both: NaN fails every comparison, and
    # Infinity orders after any clock reading, so an upper bound is what rejects
    # it. The ceiling is deliberately looser than the minter's own lifetime -- it
    # is a sanity bound held independently of the minter, not a copy of its
    # policy.
    now = time.time()
    if not now < expiry <= now + MAX_TOKEN_LIFETIME_SECONDS:
        return False
    if not payload.get("vpId") or payload.get("vpId") != vp_id:
        return False
    prefix = payload.get("prefix")
    if not prefix or uri != prefix:
        return False
    return True


def deny(status, description, message):
    return {
        "status": status,
        "statusDescription": description,
        "body": message,
        "headers": {"content-type": [{"key": "Content-Type", "value": "text/plain"}]},
    }


def lambda_handler(event, context):
    """Viewer-request handler for the /vnc/* behavior.

    Nothing here logs the token or the request URI: both identify a specific
    viewing session, and CloudFront already records the request itself.
    """
    request = event["Records"][0]["cf"]["request"]
    uri = request.get("uri", "")

    # This behavior only matches /vnc/*, but the function passes anything else
    # through untouched in case it is ever associated more widely.
    if not uri.startswith("/vnc/"):
        return request

    token = token_from_querystring(request.get("querystring", ""))
    if not token:
        return deny("401", "Unauthorized", "Authentication required")

    secret = get_signing_secret()
    if not secret:
        # Fail closed: with no secret, no token can be checked.
        print("Denying /vnc request: signing secret unavailable")
        return deny("403", "Forbidden", "Access denied")

    vp_id = vp_id_from_uri(uri)
    if not vp_id or not uri_is_plain(uri) or not verify_token(token, secret, vp_id, uri):
        return deny("403", "Forbidden", "Access denied")

    return request
'''


def create_zip_file(code_content):
    """Create a zip file containing the Lambda function code"""
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        zip_file.writestr("index.py", code_content)
    zip_buffer.seek(0)
    return zip_buffer.read()


def wait_for_function_updated(lambda_client, function_name):
    """
    Wait for $LATEST code update to finish applying (LastUpdateStatus == Successful).
    Required between successive update_function_code calls.
    """
    try:
        waiter = lambda_client.get_waiter("function_updated_v2")
        waiter.wait(
            FunctionName=function_name,
            WaiterConfig={"Delay": 5, "MaxAttempts": 60},  # up to 5 minutes
        )
        print(f"Function {function_name} $LATEST update completed (LastUpdateStatus=Successful)")
    except WaiterError as e:
        print(f"Timed out waiting for function {function_name} update to finish: {e}")
        raise


def wait_for_version_active(lambda_client, function_name, version):
    """
    Wait for a specific published Lambda version to reach State == Active.
    Required for Lambda@Edge: CloudFront rejects associations to versions
    that are still in Pending state.
    """
    try:
        waiter = lambda_client.get_waiter("function_active_v2")
        waiter.wait(
            FunctionName=function_name,
            Qualifier=version,
            WaiterConfig={"Delay": 5, "MaxAttempts": 60},  # up to 5 minutes
        )
        print(f"Function {function_name}:{version} is now Active")
    except WaiterError as e:
        print(f"Timed out waiting for function {function_name}:{version} to become Active: {e}")
        raise


def render_edge_code(signing_secret_arn, signing_secret_region):
    """The edge source with its deploy-time configuration substituted in."""
    code = EDGE_FUNCTION_CODE.replace("SIGNING_SECRET_ARN_PLACEHOLDER", signing_secret_arn)
    return code.replace("SIGNING_SECRET_REGION_PLACEHOLDER", signing_secret_region)


def create_edge_function(
    lambda_client, function_name, role_arn, signing_secret_arn, signing_secret_region
):
    """Create Lambda@Edge function in us-east-1"""
    zip_content = create_zip_file(render_edge_code(signing_secret_arn, signing_secret_region))

    try:
        response = lambda_client.create_function(
            FunctionName=function_name,
            Runtime="python3.12",
            Role=role_arn,
            Handler="index.lambda_handler",
            Code={"ZipFile": zip_content},
            Description="Lambda@Edge viewer-request authorizer for the /vnc/* behavior",
            Timeout=5,
            MemorySize=128,
            Publish=True,  # Must publish for Lambda@Edge
        )

        # When Publish=True, the response includes Version field
        # We need to construct the versioned ARN manually
        version = response.get("Version", "1")
        function_arn = response["FunctionArn"]

        # If the ARN doesn't already have a version, append it
        if not function_arn.split(":")[-1].isdigit():
            versioned_arn = f"{function_arn}:{version}"
        else:
            versioned_arn = function_arn

        print(f"Created function with version {version}, ARN: {versioned_arn}")

        # Newly-published Lambda versions start in Pending state. Lambda@Edge /
        # CloudFront associations require State=Active, so we must wait before
        # returning the versioned ARN to CloudFormation.
        wait_for_version_active(lambda_client, function_name, version)

        return versioned_arn

    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceConflictException":
            # Function already exists, update it
            return update_edge_function(
                lambda_client, function_name, signing_secret_arn, signing_secret_region
            )
        raise


def update_edge_function(lambda_client, function_name, signing_secret_arn, signing_secret_region):
    """Update existing Lambda@Edge function"""
    zip_content = create_zip_file(render_edge_code(signing_secret_arn, signing_secret_region))

    # Make sure any previous in-flight update on $LATEST has finished before we
    # try to publish a new version (otherwise we can hit ResourceConflictException).
    wait_for_function_updated(lambda_client, function_name)

    # Update function code and publish new version
    response = lambda_client.update_function_code(
        FunctionName=function_name,
        ZipFile=zip_content,
        Publish=True,  # Must publish for Lambda@Edge
    )

    # Construct versioned ARN
    version = response.get("Version", "1")
    function_arn = response["FunctionArn"]

    # If the ARN doesn't already have a version, append it
    if not function_arn.split(":")[-1].isdigit():
        versioned_arn = f"{function_arn}:{version}"
    else:
        versioned_arn = function_arn

    print(f"Updated function with version {version}, ARN: {versioned_arn}")

    # Newly-published Lambda versions start in Pending state. Lambda@Edge /
    # CloudFront associations require State=Active, so we must wait before
    # returning the versioned ARN to CloudFormation. Without this wait,
    # CloudFront rejects the association with:
    #   "The function must be in an Active state.
    #    The current state for function ...:N is Pending"
    wait_for_version_active(lambda_client, function_name, version)

    return versioned_arn


def delete_edge_function(lambda_client, function_name):
    """
    Delete Lambda@Edge function.
    Note: Lambda@Edge replicated functions can't be deleted immediately.
    They must be disassociated from CloudFront first and can take hours to replicate.
    """
    try:
        # List all versions
        versions = lambda_client.list_versions_by_function(FunctionName=function_name)

        # Delete all versions except $LATEST
        for version in versions.get("Versions", []):
            if version["Version"] != "$LATEST":
                try:
                    lambda_client.delete_function(
                        FunctionName=function_name, Qualifier=version["Version"]
                    )
                    print(f"Deleted version {version['Version']}")
                except ClientError as e:
                    error_code = e.response["Error"]["Code"]
                    if (
                        error_code == "InvalidParameterValueException"
                        and "replicated function" in str(e)
                    ):
                        print(
                            f"Version {version['Version']} is replicated - will be deleted automatically after CloudFront disassociation"
                        )
                    else:
                        print(f"Error deleting version {version['Version']}: {e}")

        # Try to delete the function
        try:
            lambda_client.delete_function(FunctionName=function_name)
            print(f"Deleted function: {function_name}")
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == "InvalidParameterValueException" and "replicated function" in str(e):
                print(
                    f"Function {function_name} is replicated - will be deleted automatically (can take 1-2 hours)"
                )
                print("This is expected behavior for Lambda@Edge functions")
                # Don't raise - this is expected and will clean up automatically
            else:
                raise

    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            print(f"Function {function_name} not found - already deleted")
        else:
            raise


def handler(event, context):
    """Custom resource handler"""
    print(f"Event: {json.dumps(event)}")

    response_data = {}
    physical_resource_id = event.get("PhysicalResourceId", "EdgeAuthFunction")

    try:
        # Get properties
        props = event["ResourceProperties"]
        function_name = props["FunctionName"]
        role_arn = props["RoleArn"]
        # The secret lives in the stack's own region; the edge function runs
        # replicated, so it has to be told where to look for it.
        signing_secret_arn = props["SigningSecretArn"]
        signing_secret_region = props["SigningSecretRegion"]

        # Lambda@Edge functions must live in us-east-1
        lambda_client = boto3.client("lambda", region_name="us-east-1")

        if event["RequestType"] in ["Create", "Update"]:
            # Create or update function
            function_arn = create_edge_function(
                lambda_client,
                function_name,
                role_arn,
                signing_secret_arn,
                signing_secret_region,
            )

            # Return the versioned ARN (required for Lambda@Edge)
            response_data["FunctionArn"] = function_arn
            physical_resource_id = function_arn

            print(f"Function ARN: {function_arn}")
            send(event, context, SUCCESS, response_data, physical_resource_id)

        elif event["RequestType"] == "Delete":
            # Delete function (may not complete immediately for Lambda@Edge)
            try:
                delete_edge_function(lambda_client, function_name)
                send(event, context, SUCCESS, response_data, physical_resource_id)
            except ClientError as e:
                error_code = e.response["Error"]["Code"]
                if error_code == "InvalidParameterValueException" and "replicated function" in str(
                    e
                ):
                    # Lambda@Edge replication - this is expected, return success
                    print(
                        "Lambda@Edge function will be deleted automatically after replication cleanup"
                    )
                    send(
                        event,
                        context,
                        SUCCESS,
                        response_data,
                        physical_resource_id,
                        reason="Lambda@Edge function marked for deletion (will complete automatically)",
                    )
                else:
                    raise

    except Exception as e:
        print(f"Error: {str(e)}")
        send(event, context, FAILED, {}, physical_resource_id, reason=str(e))
