# Chrome Extension

## How to configure

### Get the websocket endpoint

Navigate to the LMA CloudFormation stack's output and find the key **LCAWebsocketEndpoint**. Save the value. It should start with wss://, for example, `wss://abcdefg1234567.cloudfront.net/api/v1/ws`

### Create Cognito AppClient

Navigate to the LMA Cognito user pool from the AWS Management Console.  Go to App integratin tab, scroll to the **App clients and analytics** section and click **Create App Client**.

Create a new app client within the LCA Cognito user pool. Give it a unique app client name, such as `LMA-chrome-extension`.  Under Hosted UI settings, add `https://bmpfinegchjaiocjnfjfcgeohdiblomp.chromiumapp.org/` as an allowed callback url.

Once the app client is created, find the Client ID and save it.

**Note:** The `bmpfinegchjaiocjnfjfcgeohdiblomp` is a Chrome Extension ID that is generated from the public key within the manifest.json. The private key is stored in a file named `key.pem` and is required to build and submit to the Chrome app store. You must create a unique custom public, private, and extension id from the tool here: https://itero.plasmo.com/tools/generate-keypairs. Use this default one for now to test.

### Create Cognito domain

Back on the LMA Cognito user pool page, scroll to **Domain**, select **Actions** and **Create Cognito domain**. Generate a unique domain name.  

Save this domain for the next step.

### Update lma_config.json with parameters

Inside this folder, there is a file named `lma_config.json`. Populate this file with the items you were asked to save above

```
{  
  "wssEndpoint": "ws://localhost:8080/api/v1/ws",
  "clientId": "1sp17loeg9n3nnje3g5heki7pa",
  "cognitoDomain": "chris-lma-test-domain.auth.us-east-1.amazoncognito.com"
}
```

### Install the extension

From within Chrome, navigate to chrome://extensions.  On the left side, click the **Load unpacked**, navigate to the extension's folder, and click select.  This will load your extension.