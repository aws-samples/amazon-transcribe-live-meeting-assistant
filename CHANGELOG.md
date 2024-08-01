# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]


### Fixed

## [0.1.7] - 2024-08-01
### Fixed
- Use auth role for Meeting Assistant bot, and remove all permissions for unauthenticated Cognito identities #65
- Optimize costs by making Appsync API cache optional, with configurable size - default OFF. #66

## [0.1.6] - 2024-07-24
### Fixed
- Bedrock KB source links for S3 documents should be click to open #46
- Web URL missing from assistant response sources from Bedrock KB webcrawler #49
- When using Microsoft Teams, LMA browser extension closes chat window and always opens participants window #52
- Teams browser extension problem when logged in as guest account. #53
- Add note to Cognito email regarding the Chrome browser extension #55
- Meeting assistant bot voice output doesn't work. #39

## [0.1.5] - 2024-07-15
### Added
- Added initial support for Teams web client in the browser extension
- Added option to automatically create Bedrock Knowledge Base and associated S3 or Web Url datasource(s) during deployment

### Fixed
- Stack deployment now fails fast if required Bedrock models are not available or enabled in the account/region
- #44 - Stack deployment failure in AISTACK, due to node package checksum problem
- #43 - Assistant fails when Bedrock KB article is sourced from new KB web crawler data source connector


## [0.1.4] - 2024-06-08
### Added
- Improve the user experience by merging consecutive segments and render them in single line - see PR #28
- Stream Audio tab UX improvements (PR #30)
    - Added Mute/Unmute button for microphone - #29
    - Updated labels on fields and added validation
    - Defaulted meeting organiser field to logged in user's email rather than "Me"
    - Removed microphone source field (defaulted to meeting organiser)
    - Added links to open the meeting while/after recording
    - Added logic to disable fields while recording is in progress and show warning message
    - Added timestamp to meeting name to ensure id is unique
    - Updated READMEs with new field names and functionality
- Enable/disable call recording - useful if you don't want any audio recordings saved (PR #31)
- Enable configurable retention period for turn by turn transcription - useful if you want to keep the meeting summary, but not the line by line transcription (PR #31)
- Enable configurable retention for CloudWatch logs (PR #31)
### Fixed
- #33 - Fix/active speaker assignment not for mic channel (PR #34)
- Streamline Websocket server logs
- #25 - Fix Updating Participant Name on Stream Audio Page does not reflect in the meeting transcript
- #24 - Fix TEST ALL in QnABot is continuously putting file version into the S3 bucket (PR #26)
- #35 - Fix Browser extension intermittently silently fails to authenticate (PR #35)

## [0.1.3] - 2024-05-22
### Fixed
- #6 - Fixed multi languageID segment overwrite issue (PR #23)

## [0.1.2] - 2024-05-10
### Added
- Added option for Bedrock LLM (without knowledge bases) to be used as the meeting assistant service for 'OK Assistant' and 'Ask Assistant' responses. 
- Added option for single language auto-detection - using Amazon Transcribe's 'Identify Language'.
- Added option for multiple language auto-detection - using Amazon Transcribe's 'Identify Multiple Languages'.
### Fixed
- Added `&` to the previous defense against Meeting Names / IDs with special characters that are not URL safe, by replacing with pipe character `|` in the browser extension when starting the streaming. PR #10
- Fix for #1 - "Stream Audio" tab stops working after a stack update when AssistantWakePhraseRegEx is modified. PR #11
- Fix for #2 - Incorrect Chime speaker name attribution when muting - PR #19
- Fix for #3 - Add a CloudFormation rule to require `BedrockKnowledgeBaseId` parameter to be provided when BEDROCK_KNOWLEDGE_BASE is chosen as the meeting assistant service.
- Fix for #4 - Chrome extension bug causing meeting topic to be continually overwritten
- Fix for #13 - Longer CloudFormation stack names cause errors in length of Lambda function names.
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
[0.1.7]: https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/releases/tag/v0.1.7
[0.1.6]: https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/releases/tag/v0.1.6
[0.1.5]: https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/releases/tag/v0.1.5
[0.1.4]: https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/releases/tag/v0.1.4
[0.1.3]: https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/releases/tag/v0.1.3
[0.1.2]: https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/releases/tag/v0.1.2
[0.1.1]: https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/releases/tag/v0.1.1
[0.1.0]: https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/releases/tag/v0.1.0
