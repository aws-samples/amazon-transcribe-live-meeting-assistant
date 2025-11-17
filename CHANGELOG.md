# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.21] - 11/17/25

### Added
- Strands-based Meeting Assistant Tools
- noVNC Support for Real-Time Virtual Participant Viewing and Interaction
- Claude Global Models
- Tool for meeting chronology with DynamoDB query for finding last meeting

### Changed
- Update chime meeting ending functionality
- Filters for virtual participant enhancements

### Fixed
- Fix zoom speaker detection
- Zoom bugfix VNC wording
- Fix: Virtual Participant VNC Routing for Multiple Concurrent Sessions
- Remove ARN being detected in history for code defender
- Teams captcha fix
- Fixing logging bucket issue
- Add detection and delay for Amazon Zoom to login with VNC viewer
- Increase VNC connect delay for healthy for Zoom connect failure
- Fix virtual participant issue
- ECR vulnerabilities in VP stack

## [0.2.20] - 2025-11-04
### Fixed
 - Github #199 issue: Fixed: Deployment issues due to a logging bucket name missing issue. 

## [0.2.20] - 2025-10-24
### Added
- Virtual Participant filtering and table
- User-based access control (UBAC) enhancements for Virtual Participants with improved security and permissions
- Terminate/end scheduled Virtual Participant tasks functionality with automated cleanup

## [0.2.19] - 2025-10-17
### Added
- Virtual Participant scheduling feature for future meetings
- Meeting invitation parsing through Bedrock AI for auto-filling forms
- Copy to clipboard feature for meeting summaries

## [0.2.18] - 2025-10-13
### Fixed
- Github #197 issue : Fixed: Virtual Participant functionality not working with native Chime/Zoom applications in version 0.2.17

## [0.2.17] - 2025-10-10
### Added
- Webex Virtual Participant support (joins Teams, Zoom, Chime, and Meet)
- Claude Sonnet 4.5 support for meeting summaries and meeting assistant
- AWS Strands agent as a lightweight alternative to QnABot for meeting assistance with new STRANDS_BEDROCK option

### Changed
- Enhanced security posture with comprehensive infrastructure and code improvements
- Added optional IAM permissions boundary support (similar to IDP project)
- Created centralized CustomerManagedEncryptionKey resource for consistent encryption across all services

## [0.2.16] - 2025-10-01
### Fixed
- Github #196 issue - Fixed QnABot deployment permissions issue.

## [0.2.15] - 2025-09-26
### Added
- Added support for Teams Meeting application

### Changed
- Migrated Virtual Participant from Python sdk to Node sdk.

### Fixed

## [0.2.14] - 2025-09-12
### Added
- Virtual Participant user enhancements including status tracking, notifications for tracking virtual participant, and option to end virtual participant

### Fixed
- Meeting deletion now properly removes Virtual Participant traces

## [0.2.14] - 2025-09-12
### Added
- Virtual Participant user enhancements including status tracking, notifications for tracking virtual participant, and option to end virtual participant

### Fixed
- Meeting deletion now properly removes Virtual Participant traces

## [0.2.13] - 2025-08-28
### Added
- Virtual Participant UI improvements including success modal and enhanced error handling

### Changed
- Updated license to MIT-0 for improved open source compatibility
- ESLint and Prettier code formatting improvements for UI components

### Fixed
- Zoom Virtual Participant issue - improved element waiting logic and removed unnecessary delays
- Deployment name handling fixes


## [0.2.12] - 2025-08-21
### Added
- Support for newest Amazon Bedrock models including inference profiles
- Auto-scroll behavior during meetings - now stops and switches to manual scroll when user scrolls up to read previous transcripts
- New 'Deployment Info' details added to the UI showing stack name, build date, and version.

### Changed
- Migrated from complex update-version.sh script approach to simpler token-based versioning system, eliminating version mismatches and reducing maintenance overhead
- Updated Lambda runtime to Node.js 22 across multiple stacks
- Favicon replaced for better branding

### Fixed
- Virtual Participant Dockerfile - fixed Chromium font dependencies issue that was causing CloudFormation deployment failures


## [0.2.11] - 2025-07-03
### Added
- Adding model initialization parameter for when Amazon Bedrock model access might not be setup e.g. AWS workshops. Enables deployment of LMA without Amazon Bedrock model access. #185


## [0.2.10] - 2025-06-20
### Added
- Add DOCX export functionality and increase LLM chat history limit. #183
### Fixed 
- When deploying LMA, the VirtualParticipantStack fails to create due to BuildCustomResourceFunction resource. #184

## [0.2.9] - 2025-02-09
### Fixed 
- Zoom browser extension, active speaker not identified when video share or screen share is on bug fix #174

## [0.2.8] - 2025-02-07
### Fixed 
- user identification has not working with chrome extension for teams - PR #170
- Changed browser extension icons to a "person in a meeting room" - PR #167

## [0.2.7] - 2024-11-23
### Added
- Added ability to remove 'sharedWith' users - PR #158
- Added delete meetings functionality - PR #158
- Integration for Google Meet in LMA browser extension - PR #159
### Fixed 
- Improvements to meetings sharing user experience - PR #158
- Refactored UI to use common modules to avoid code duplication - PR #158

## [0.2.6] - 2024-11-01
### Fixed
- Fixed a bug that allowed non-Admin users to see other user call details if they had (or could guess) the callId.
- Fixed a bug that prevented calls from being shared for live calls.
- Added share meeting button to call split panel and call details pages.

## [0.2.5] - 2024-10-25

### Added
- User Based Access Control is enhanced to allow users to share their meetings with other users. PR #145
- Add support for new Anthropic Claude 3.5 Sonnet v2 - PR #147

### Fixed
- Updated zoom code to address issue for leaving a meeting, continued effort on participant recognition - PR #148


## [0.2.4] - 2024-10-20

### Added
- Knowledge base of meeting transcripts #129
- Meetings Query Tool for running GenAI queries across the new meetings knowledge base [README](./README.md#meetings-query-tool---query-past-meetings-from-the-transcript-knowledge-base)

### Fixed
- Stops transcribing calls without error messaging - added exception catch and retry for Transcribe sessions - Issue #137
- Virtual Participant meeting won't open in UI if meeting name has &, /, or + symbols #142

## [0.2.3] - 2024-10-11

### Added
- Allow meeting assistant to perform custom actions using a Bedrock Agent #128

### Fixed
- Fixes for differences between Zoom and Zoom Enterprise - PR #132

## [0.2.2] - 2024-10-03

### Added
- Ability to apply optional Bedrock Guardrail when MeetingAssistant is BEDROCK_KNOWLEDGE_BASE or BEDROCK_LLM - Issue #53 

### Fixed
- When using Virtual Participant (preview) no audio recording is created. #126
- Use selected Transcribe language for virtual-participant - PR #118 
- Updated QnABot nested stack to QnABot v6.1.1 - PR #119
- Publish script fails to detect make failure in lca-ai-stack during build - Issue #111
- When publishing, AISTACK `make` fails to find bash with new version of gnu make (4.3) - Issue #110
- Muliple dependabot PRs
- Upgrade all python Lambda functions to python3.12 (latest)
- Publish script hangs in lma-ui-stack in sam build when using arm64 container image, on new EC2 and Cloud9 instances #124

## [0.2.1] - 2024-08-29

### Added
- Support for Anthropic Claude 3.5 Sonnet #94

### Fixed
- With Q Business as the Meeting Assistant, the ASK ASSISTANT button responses are missing meeting transcript context #97
- Non admin users are unable to see meetings started with Virtual Participant #98


## [0.2.0] - 2024-08-24

### Notes
- When you update an existing stack from v0.1.x to v0.2.x the existing admin user is recreated. You are emailed a temporary password and must set a new permanent password as you did when you first deployed the stack. See [User Based Access Control (Preview)](./lma-ai-stack/README_UBAC.md) for more information, and recommendations on how to differentiate the admin username/email from a non-admin username/email (e.g joe+admin@acme.com vs joe@acme.com) if you are configuring LMA for multiple users.

### Added
- User Based Access Control (UBAC) (Preview) adds multi-user access where each user can access only meetings that they initiated. #67
- Amazon Q Business can now be used as the meeting assistant knowledge base service. #68

### Fixed
- Virtual Participant is status "In Progress" after Chime meeting ended and no call summary generated #84
- Speaker attribution lacks fidelity when multiple users are talking #92
- Knowledge base citation source links occasionally blank #93
- Use version number in browsers extension download zip file name to avoid download of older version due to cache.
- Miscellaneous security patches from dependabot


## [0.1.10] - 2024-08-15

### Added

- Initial support for WebEx web client in the browser extension PR #81
- Download button for exporting call summary and call transcript PR #80
- Optional specialized prompts for Healthcare use cases (SOAP/BIRP notes, etc.) by selecting 'Healthcare' from new 'Business Domain' parameter - PR #20

## [0.1.9] - 2024-08-05

### Fixed

- #76 Support optional deployment using existing VPC/subnets
- #71 Introduce 'Virtual Participant' (Preview) [VP README](./lma-virtual-participant-stack/README.md)
- #155 Missing call transcript download button

## [0.1.8] - 2024-08-01

### Fixed

- Remove unused KMS keys #72

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
