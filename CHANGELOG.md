# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.2] - 2024-05-10
### Added
- Added option for Bedrock LLM (without knowledge bases) to be used as the meeting assistant service for 'OK Assistant' and 'Ask Assistant' responses. 
- Added option for single language auto-detection - using Amazon Transcribe's 'Identify Language'.
- Added option for multiple language auto-detection - using Amazon Transcribe's 'Identify Multiple Languages'.
### Fixed
- Added `&` to the previous defense against Meeting Names / IDs with special characters that are not URL safe, by replacing with pipe character `|` in the browser extension when starting the streaming. PR #10
- Fix for #1 - "Stream Audio" tab stops working after a stack update when AssistantWakePhraseRegEx is modified. PR #11
- Fix for #13 - Longer CloudFormation stack names cause errors in length of Lambda function names.
- Fix for #3 - Add a CloudFormation rule to require `BedrockKnowledgeBaseId` parameter to be provided when BEDROCK_KNOWLEDGE_BASE is chosen as the meeting assistant service.
- Fix for #4 - Chrome extension bug causing meeting topic to be continually overwritten
### Changed
- Downsize web socket server ecs-fargate task for improved cost efficiency. PR #12
- Browser extension now displays release version number
- Websocket server now sends audio in 100ms chunks to Transcribe (best practice)

 
## [0.1.1] - 2024-04-19
### Fixed
- Added defense against Meeting Names / IDs with special characters `/?#%+` that are not URL safe, by replacing with pipe character `|` in the browser extension when starting the streaming.


## [0.1.0] - 2024-04-17
### Added
- Initial release


[Unreleased]: https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/compare/main...develop
[0.1.2]: https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/releases/tag/v0.1.2
[0.1.1]: https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/releases/tag/v0.1.1
[0.1.0]: https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/releases/tag/v0.1.0
