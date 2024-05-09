# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.2] - TBD
### Added
- New MeetingAssistService option to allow Bedrock LLM (without knowledge bases) to be used for 'OK Assistant' and 'Ask Assistant' responses.
### Fixed
- Added `&` to the previous defense against Meeting Names / IDs with special characters that are not URL safe, by replacing with pipe character `|` in the browser extension when starting the streaming. PR #10
- Fix for #1 - "Stream Audio" tab stops working after a stack update when AssistantWakePhraseRegEx is modified. PR #11
- Fix for #13 - Longer CloudFormation stack names cause errors in length of Lambda function names.
- Fix for #3 - Add a CloudFormation rule to require `BedrockKnowledgeBaseId` parameter to be provided when BEDROCK_KNOWLEDGE_BASE is chosen as the meeting assistant service.  
### Changed
- Downsize web socket server ecs-fargate task for improved cost efficiency. PR #12

 
## [0.1.1] - 2024-04-19
### Fixed
- Added defense against Meeting Names / IDs with special characters `/?#%+` that are not URL safe, by replacing with pipe character `|` in the browser extension when starting the streaming.


## [0.1.0] - 2024-04-17
### Added
- Initial release


[Unreleased]: https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/compare/v0.1.2...develop
[0.1.2]: https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/releases/tag/v0.1.2
[0.1.1]: https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/releases/tag/v0.1.1
[0.1.0]: https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/releases/tag/v0.1.0
