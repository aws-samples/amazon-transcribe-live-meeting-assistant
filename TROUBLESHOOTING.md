# Troubleshooting

If the CloudFormation stack deployment fails, use the Events tab to find the first CREATE_FAILED message.

If the CREATE_FAILED message refers to a nested stack, then find that nested stack, and navigate to the first CREATE_FAILED message. NOTE - if your stack rolled back already, the nested stacks are automatically deleted for you, so to find them change the Stacks filter from `Active` to `Deleted`.

Stacks may be nested several levels deep, so keep drilling down till you find the first resource that failed in the more deeply nested stack.

Examine the Status reason column for clues to the failure reason.
