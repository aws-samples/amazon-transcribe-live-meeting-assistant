# Chrome Extension

## How to configure

1. Download and clone this repository. 

2. Navigate to the LMA CloudFormation stack's output,  find the **ChromeExtensionConfigJson** and copy the value, which is json with 4 key value pairs. 

3. Inside the `lma-chrome-extension/public` folder, there is a file named `lma_config.json`. Populate this file with the value you just copied. It should look something like below:

```
{  
  "wssEndpoint": "ws://abcdefg1234567.cloudfront.net/api/v1/ws",
  "clientId": "1a2b3c4d5e6f7g8h9i0j1k2l3m",
  "cognitoDomain": "https://lma-1234567899.auth.us-east-1.amazoncognito.com"
}
```

### Build the extension

Build the extension by running `npm install` and then `npm run build` from within the `lma-chrome-extension` folder. The compiled extension files will be located within the `lma-chrome-extension/build` folder.

### Install the extension

From within Chrome, navigate to chrome://extensions.  On the left side, click the **Load unpacked**, navigate to the `lma-chrome-extension/build` folder, and click select.  This will load your extension.

## How to use

Once you have the extension installed, login with your LMA credentials.

Navigate to your meeting platform's page. If you already have it loaded, please reload the page. 

Select the `Start Listening` button.

