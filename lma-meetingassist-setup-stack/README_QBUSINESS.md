# Amazon Q Business integration in LMA

Amazon Q is a new generative AI-powered application that helps users get work done. Amazon Q can become your tailored business expert and let you discover content, brainstorm ideas, or create summaries using your company’s data safely and securely. For more information see: [Introducing Amazon Q, a new generative AI-powered assistant](https://aws.amazon.com/blogs/aws/introducing-amazon-q-a-new-generative-ai-powered-assistant-preview). This feature allows your users to converse with Amazon Q using Live Meeting Assistant to ask questions and get answers based on company data.

## Overall Flow of integrating with Q Business in LMA

1. Start with an existing working QBusiness application with active subscriptions for LMA user(s)
2. First, install new LMA stack or update LMA
2. Then create an Identity Center Application for your Q Business application
3. Finally, update the LMA stack with the ARN of your Identity Center application

The below instructions walk through this implementation step-by-step, including the manual creation of the IDC Application.

## Deploy Amazon Q (your business expert) as a fallback source of answers

1. Before proceeding, you need an existing Q Business application. Each LMA user, including the admin user, must have a valid subscription for the Q Business application using IAM Identity Center (IDC), with email addresses that match the emails that have been (or will be) used for their LMA user accounts. Please reference the AWS docs for creating a new [Q Business application](https://docs.aws.amazon.com/amazonq/latest/qbusiness-ug/create-application.html).
    1. `Web Experience URL`
    2. `Application ID` (a UUID that looks like ac12345d-0f79-404d-82e0-d920f1a1234c)
2. Deploy a new LMA stack, or update an existing LMA stack:
    1. For `Meeting Assist Service` Select 'Q_Business (use existing)'
    2. For `AmazonQAppId` enter your existing Amazon Q Application ID (a UUID)
    3. Initially leave `IDC Application ARN`empty - later, after you create the IDC Application, you will update the stack with its ARN.
3. Once the stack update or creation has completed, open the `Outputs` tab of the CloudFormation stack and note the following output values:
    1. CognitoUserPoolClientId
    2. CognitoUserPoolTokenIssuerUrl
    3. QBusinessLambdaHookFunctionRoleArn
4. The Cognito user pool created by the Live Meeting Assistant needs to be added as **Trusted token issuer** to Identity Center by doing the following steps
    1. Open the IAM Identity Center console, and from the left navigation menu choose `Settings`, then choose the tab labelled `Authentication`, then `Create trusted token issuer`
    2. For `Issuer URL` enter the `CognitoUserPoolTokenIssuerUrl`
    3. For `Trusted token issuer name` enter an name, e.g. "LMA"
    4. For `Mapping Attributes`, select `email`.
    5. Choose `Create Trusted Token Issuer`
        ![Issuer](../images/token-issuer.PNG)
5. Create a custom application in Identity Center to handle the connection between your Q Business application and your Cognito pool:
    1. In the IAM Identity Center console, from from the left navigation menu choose `Applications` then `Add application`
    2. Select `I have an application I want to set up` and `OAuth 2.0` on the next page for Selecting Application type, then `Next`
    3. For `Application URL`, provide the **Web experience URL** of your Q Business application (if you have a custom domain for your Q Business application, use the URL of that domain). You can either opt to assign specific users/groups to this application or allow any Identity Center users/groups to access the application. Your Q Business subscriptions will still apply however so only users with a subscription can successfully chat with the application. Then hit `Next`.
    4. Select the Trusted token issuer that was created in the previous section of these instructions. For `Aud claim` enter the `CognitoUserPoolClientId` obtained from the LMA Cloudformation stack outputs above. Then `Next`
    5. For application credentials, enter the `QBusinessLambdaHookFunctionRoleArn` obtained from the LMA Cloudformation stack outputs above. Then `Next`
    6. Choose `Submit` to create the application.
    7. Open the new application from the `Customer managed` tab of Identity Center applications. 
    8. Choose `Specify trusted applications`, `All applications for service with same access` and select `Q Business` from the list of applications for trust. When complete your app should appear similar to the below configuration. Note the application ARN value.
        ![IDCApp](../images/icd-application.png)

6. Return to CloudFormation, select the LMA stack, choose `Update`, then `Use existing template`. 
7. This time, for `IDC Application ARN` enter the ARN of the IDC application created above. 
8. You have successfully created a trust relationship between LMA and Identity Center

**It is necessary that LMA users have access to the configured Amazon Q Business application, and that their LMA username / email address matches their registered Amazon Q Business user / Identity Center user email. Any LMA user that does not have a matching Amazon Q Business subscription will receive error messages when attempting to use LMA meeting assistant features.**

See the [Cognito documentation](https://docs.aws.amazon.com/cognito/latest/developerguide/managing-users.html) for more information about replicating users to Coginto from your own identity provider.

## After your Amazon Q Plugin stack is deployed
After setup, Live Meeting Assistant will use Q Business as a fallback for answering questions asked by the Meeting Assist Bot and the 'Okay Assistant' queries asked during meetings. 