# User Based Access Control (Preview)

The latest release (0.2.0) of LMA has a new User-Based Access Control (UBAC, beta) feature. Previous versions of LMA 
supported multiple users (if email domain is enabled) by allowing any user in your organization to sign-up for access. 
However, it lacked fine-grained access controls in the UI. This meant that every user was able to see every other users'
meetings. UBAC allows each user to only see their own meetings to ensure user privacy and reduce noise. With UBAC, meetings 
are personalized for each user, without any disruption in existing capabilities (see [Limitations](#limitations) for 
how this could change the experience for existing users during an upgrade). 

The only exception to this is the user that created/updated the stack who is automatically delegated a Cognito "Admin" role
and is able to see all the calls. You may need this for any administrative or troubleshooting tasks. If this is not desired,
you can either delete the Cognito User associated with the stack creation/update or remove the user from the "Admin" role. 
For information on managing users in your user pool see 
[Managing users in your user pool](https://docs.aws.amazon.com/cognito/latest/developerguide/managing-users.html)

## New LMA stack deployment

If you are deploying LMA for the first time, under "Admin Email Address", enter an email address that you would 
use only to log in occasionally to troubleshoot any issues users may be experiencing. If you need to use the same
email address for both administrative and personal use, we recommend that you enter your email address in the 
jdoe+admin@example.com format. Once the stack successfully deploys, you can sign-up using the Web UI link with your 
email address (jdoe@example.com format).

### Changing your Admin user password
You will receive an email to the address you entered under "Admin Email Address". This has the temporary password. Follow
these instructions to login to the LMA Web UI and changing your password:
1. Navigate to CloudFormation and ensure the LMA stack status shows CREATE_COMPLETE
2. Navigate to your Stack's Outputs section and copy the value associated with the ApplicationCloudfrontEndpoint key.
3. This is your LMA Web UI.
4. Log in to the Web UI with the temporary password and create a new password when prompted.

## Updating an existing LMA stack (from 0.1.9 or prior versions)

As part of the upgrade to 0.2.0, the existing Cognito user pool will be deleted. This means that you (the administrator)
and the other users will have to sign-up for a user again. You may have initially deployed the stack using the 
jdoe@example.com format. We recommend that you change the "Admin Email Address" value to the jdoe+admin@example.com format. 
See [New LMA stack deployment](#new-lma-stack-deployment) for more details.

### Existing non-Admin users
Existing users will have to re-create a new account by visiting the Web UI. Existing users will lose access to their 
previous meetings. 

### Existing Admin user 
You will receive an email to the address you entered under "Admin Email Address" with a temporary password. Log in to 
the Web UI with the temporary password and create a new password when prompted. If you do not have the Web UI link from 
your previous deployment, follow the instructions under
[Changing your Admin user password](#changing-your-admin-user-password)

As an administrator, you have access to all the previous meetings. There is no change in functionality for this user.

## Limitations
- This is a beta feature and may have bugs. Use it with caution. If you encounter any issues, please open a GitHub issue.
- This feature upgrade (if upgrading from versions 0.1.9 or earlier) a breaking change. Existing users will not be able 
  to see their previously recorded meetings. A future release will enable authorized administrators to share meetings 
  other users/participants at which time you will be able to see your meetings created in previous versions 
  (versions 0.1.9 or earlier) natively in the Web UI. In the interim, administrators can see your meetings and will be 
  able to retrieve the transcripts, summaries and other information manually.
- Users (unless they are designated as "Admin" via Cognito - default for the user created with the stack) will not be
  able to share their meetings with other users. A future release will address this by providing an option to share 
  meetings with other users.
- Service limits apply. For example, Amazon Transcribe Streaming by default only allows you to stream up to 25 calls at
  a given time. You can request an increase to this through support. For Amazon Transcribe quotas and limits see
  [Amazon Transcribe endpoints and quotas](https://docs.aws.amazon.com/general/latest/gr/transcribe.html). Similar limits may apply to other services.

## Developer testing / troubleshooting notes

- To run LMA UI locally, see [How to run LMA UI Locally](./source/ui/README.md)
- To contribute to the solution, report bugs or issues, see [Contributing Guidelines](../CONTRIBUTING.md)