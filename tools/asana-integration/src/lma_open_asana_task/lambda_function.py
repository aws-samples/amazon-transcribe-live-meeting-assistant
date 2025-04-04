import json, logging, os
import boto3
import datetime
import uuid
import requests

from datetime import datetime, timedelta
from botocore.exceptions import ClientError

DYNAMODB_EXPIRATION_IN_DAYS = 90 

logger = logging.getLogger()

def get_ttl():
    return int((datetime.utcnow() + timedelta(days=int(DYNAMODB_EXPIRATION_IN_DAYS))).timestamp())

def lambda_handler(event, context):
    print (event)
    # print("Event: ", json.dumps(event))
    agent = event['agent']
    actionGroup = event['actionGroup']
    function = event['function']
    parameters = event.get('parameters', [])

    customer_phone_number = event["CustomerPhoneNumber"]
    case_description = event.get("CallSummaryText", "No summary available.")
    
    phone = "'%%%s%%%s%%%s%%'" % (customer_phone_number[-10:-7], customer_phone_number[-7:-4], customer_phone_number[-4:])
    response = create_case(phone, case_description)

    salesforce_url = "https://aws-31d-dev-ed.develop.lightning.force.com/lightning/r/Case/" + response["id"] + "/view"
    transcript = "Case created. For details visit the <a href=" + salesforce_url + " target=_blank rel=noopener noreferrer>Salesforce Service Portal</a>"
  
    responseBody = {
      "TEXT": {
        "body": transcript
      }
    }

    action_response = {
        'actionGroup': actionGroup,
        'function': function,
        'functionResponse': {
            'responseBody': responseBody
        }
    }
    function_response = {'response': action_response, 'messageVersion': event['messageVersion']}
    print("Response: {}".format(function_response))
    return function_response

def get_arg(kwargs, name):
  if name not in kwargs:
    msg = "'%s' enviroment variable is missing"
    logger.error(msg)
    raise Exception(msg)
  return kwargs[name]
  
def create_case(phone, case_description):
    session = boto3.session.Session()
    secrets = {}
    secrets_manager_client = session.client(
        service_name="secretsmanager"
    )
    sf_credentials_secrets_manager_arn = get_arg(os.environ, "SF_CREDENTIALS_SECRETS_MANAGER_ARN")

    logger.info("Loading credentials")
    secrets = json.loads(secrets_manager_client.get_secret_value(SecretId=sf_credentials_secrets_manager_arn)["SecretString"])

    password = secrets["Password"] + secrets["AccessToken"]
    consumer_key = secrets["ConsumerKey"]
    consumer_secret = secrets["ConsumerSecret"]
    auth_token = secrets["AuthToken"] if "AuthToken" in secrets else ''
    headers = { 
        'Authorization': 'Bearer %s' % auth_token,
        'Content-Type': 'application/json'
    }
    logger.info("Credentials Loaded")
    
    version=get_arg(os.environ, "SF_VERSION")
    host=get_arg(os.environ, "SF_HOST")
    username=get_arg(os.environ, "SF_USERNAME")

    login_host = host
    request = Request()
    auth_data = {
        'grant_type': 'password',
        'client_id': consumer_key,
        'client_secret': consumer_secret,
        'username': username,
        'password': password
    }

    if get_arg(os.environ, "SF_PRODUCTION").lower() == "true":
        set_production()

    logger.info("Salesforce: Query")
    
    query = "SELECT Id, Name from Contact WHERE Phone LIKE " + phone 
    
    url = '%s/services/data/%s/query' % (host, version)
    resp = makeRequest(request.get, headers, login_host, secrets, secrets_manager_client, sf_credentials_secrets_manager_arn, auth_data, **{"url": url, "params":{'q':query}})
    data = resp.json()
    print(data['records'][0]['Id'])

    post_data = {"Status": "New", "Origin": "Phone", "Description": case_description, "ContactId": data['records'][0]['Id']}

    url = '%s/services/data/%s/sobjects/%s/' % (host, version, "Case")
    resp = makeRequest(request.post, headers, login_host, secrets, secrets_manager_client, sf_credentials_secrets_manager_arn, auth_data, **{"url": url, "data": post_data})
    # print (headers, login_host)
    print(resp.json())
    data = resp.json()

    return data

def makeRequest(requestMethod, headers, login_host, secrets, secrets_manager_client, sf_credentials_secrets_manager_arn, auth_data, **kwargs):
    try:
      return requestMethod(**kwargs, headers=headers)
    except Exception as e:
      # try re-fetching auth token
      logger.info("Retrieving new Salesforce OAuth token")
      headers = { 'Content-Type': 'application/x-www-form-urlencoded' }
      request = Request()

      resp = request.post(url=login_host+"/services/oauth2/token", params=auth_data, headers=headers, hideData=True)
      data = resp.json()
      auth_token = secrets["AuthToken"] = data['access_token']
      headers = kwargs['headers'] = { 
        'Authorization': 'Bearer %s' % auth_token,
        'Content-Type': 'application/json'
      }
      try:
        secrets_manager_client.put_secret_value(SecretId=sf_credentials_secrets_manager_arn, SecretString=json.dumps(secrets))
      except ClientError as e:
        # LimitExceededException occurs when there are too many versions of a secret in SecretsManager.
        # Secret versions are cleaned up in the background but sometimes this doesn't happen fast enough.
        # In this case, the error is safe to ignore.
        if e.response['Error']['Code'] == 'LimitExceededException':
          logger.error(str(e))
        else:
          raise e
      return requestMethod(**kwargs)

class Request:
  def post(self, url, headers, data=None, params=None, hideData=False):
    logger.info('POST Requests:\nurl=%s' % url)
    if not hideData:
      logger.info("data=%s\nparams=%s" % (data, params))
    r = requests.post(url=url, data=json.dumps(data), params=params, headers=headers)
    if not hideData:
      logger.info("Response: %s" % r.text)
    return __check_resp__(r)

  def delete(self, url, headers):
    logger.info("DELETE Requests:\nurl=%s" % url)
    r = requests.delete(url=url, headers=headers)
    logger.info("Response: %s" % r.text)
    return __check_resp__(r)

  def patch(self, url, data, headers):
    logger.info("PATCH Requests:\nurl=%s\ndata=%s" % (url, data))
    r = requests.patch(url=url, data=json.dumps(data), headers=headers)
    logger.info("Response: %s" % r.text)
    return __check_resp__(r)

  def get(self, url, params, headers):
    logger.info("GET Requests:\nurl=%s\nparams=%s" % (url, params))
    r = requests.get(url=url, params=params, headers=headers)
    logger.info("Response: %s" % r.text)
    return __check_resp__(r)

def __check_resp__(resp):
  if resp.status_code // 100 == 2: 
    return resp
  
  if resp.status_code == 401:
    raise Exception("")
  
  data = resp.json()
  if 'error' in data:
    msg = "%s: %s" % (data['error'], data['error_description'])
    logger.error(msg)
    raise Exception(msg)
  
  if isinstance(data, list):
    for error in data:
      if 'message' in error:
        msg = "%s: %s" % (error['errorCode'], error['message'])
        logger.error(msg)
        raise Exception(msg)

  msg = "request returned status code: %d" % resp.status_code
  logger.error(msg)
  raise Exception(msg)

