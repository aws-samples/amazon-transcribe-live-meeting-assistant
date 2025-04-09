# LMA Bedrock Agent Stack

This stack creates a Bedrock Agent for use with the Live Meeting Assistant (LMA) application. The agent can be configured with various action groups to enable business application integrations.

## Available Integrations

### 1. SendMessage Action Group (Default)
Allows the agent to send messages via SNS to email addresses or SMS numbers.

### 2. Salesforce Integration
When enabled, allows the agent to create opportunities in Salesforce.

**Required Configuration:**
- Update the Salesforce credentials in the AWS Secrets Manager secret created by the stack
- The secret should contain:
  ```json
  {
    "instance_url": "https://your-instance.my.salesforce.com",
    "client_id": "your_client_id",
    "client_secret": "your_client_secret",
    "username": "your_username",
    "password": "your_password",
    "security_token": "your_security_token"
  }
  ```

### 3. Jira Integration
When enabled, allows the agent to create issues in Jira.

**Required Configuration:**
- Update the Jira credentials in the AWS Secrets Manager secret created by the stack
- The secret should contain:
  ```json
  {
    "url": "https://your-domain.atlassian.net",
    "email": "your_email@example.com",
    "api_token": "your_api_token"
  }
  ```

### 4. Asana Integration
When enabled, allows the agent to create tasks in Asana.

**Required Configuration:**
- Update the Asana credentials in the AWS Secrets Manager secret created by the stack
- The secret should contain:
  ```json
  {
    "access_token": "your_access_token",
    "workspace_gid": "your_workspace_gid"
  }
  ```

## Deployment Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| BedrockModelID | The Bedrock model ID to use for the agent | anthropic.claude-3-haiku-20240307-v1:0 |
| BedrockKnowledgeBaseId | Existing knowledge base ID (leave blank to create new) | "" |
| KnowledgeBaseBucketName | S3 bucket with documents for knowledge base | "" |
| InputDocumentUploadFolderPrefix | S3 prefixes for knowledge base documents | "" |
| WebCrawlerURLs | URLs to crawl for knowledge base | "" |
| WebCrawlerScope | Scope of web crawling | DEFAULT |
| SNSEmailAddress | Email address for SendMessage notifications | (required) |
| EnableSalesforceIntegration | Enable Salesforce integration | false |
| EnableJiraIntegration | Enable Jira integration | false |
| EnableAsanaIntegration | Enable Asana integration | false |

## Stack Outputs

| Output | Description |
|--------|-------------|
| AgentId | The ID of the created Bedrock Agent |
| AgentAliasId | The ID of the created Bedrock Agent Alias |
| SNSTopicForAgentMessages | The SNS topic name for agent messages |
| SalesforceSecretName | Secret name for Salesforce credentials (if enabled) |
| JiraSecretName | Secret name for Jira credentials (if enabled) |
| AsanaSecretName | Secret name for Asana credentials (if enabled) |

## Adding Custom Integrations

To add additional integrations:

1. Create a new Lambda function for your integration
2. Add the necessary IAM roles and permissions
3. Update the Bedrock Agent definition to include a new action group
4. Add the Lambda permission for Bedrock to invoke your function

See the existing integrations in the template for examples.
