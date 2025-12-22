/* eslint-disable */
// this is an auto generated file. This will be overwritten

export const initOAuthFlow = /* GraphQL */ `
  mutation InitOAuthFlow($input: InitOAuthFlowInput!) {
    initOAuthFlow(input: $input) {
      authorizationUrl
      state
      success
      error
    }
  }
`;

export const handleOAuthCallback = /* GraphQL */ `
  mutation HandleOAuthCallback($input: OAuthCallbackInput!) {
    handleOAuthCallback(input: $input) {
      success
      serverId
      error
    }
  }
`;

export const createCall = /* GraphQL */ `
  mutation CreateCall($input: CreateCallInput!) {
    createCall(input: $input) {
      CallId
      Owner
      SharedWith
    }
  }
`;

export const updateCallStatus = /* GraphQL */ `
  mutation UpdateCallStatus($input: UpdateCallStatusInput!) {
    updateCallStatus(input: $input) {
      PK
      SK
      CreatedAt
      UpdatedAt
      ExpiresAfter
      CallId
      CustomerPhoneNumber
      SystemPhoneNumber
      Status
      RecordingUrl
      PcaUrl
      TotalConversationDurationMillis
      AgentId
      Metadatajson
      CallCategories
      IssuesDetected
      CallSummaryText
      Owner
      SharedWith
    }
  }
`;

export const updateCallAggregation = /* GraphQL */ `
  mutation UpdateCallAggregation($input: UpdateCallAggregationInput!) {
    updateCallAggregation(input: $input) {
      PK
      SK
      CreatedAt
      UpdatedAt
      ExpiresAfter
      CallId
      CustomerPhoneNumber
      SystemPhoneNumber
      Status
      RecordingUrl
      PcaUrl
      TotalConversationDurationMillis
      AgentId
      Metadatajson
      CallCategories
      IssuesDetected
      CallSummaryText
      Owner
      SharedWith
    }
  }
`;

export const updateRecordingUrl = /* GraphQL */ `
  mutation UpdateRecordingUrl($input: UpdateRecordingUrlInput!) {
    updateRecordingUrl(input: $input) {
      PK
      SK
      CreatedAt
      UpdatedAt
      ExpiresAfter
      CallId
      CustomerPhoneNumber
      SystemPhoneNumber
      Status
      RecordingUrl
      PcaUrl
      TotalConversationDurationMillis
      AgentId
      Metadatajson
      CallCategories
      IssuesDetected
      CallSummaryText
      Owner
      SharedWith
    }
  }
`;

export const updatePcaUrl = /* GraphQL */ `
  mutation UpdatePcaUrl($input: UpdatePcaUrlInput!) {
    updatePcaUrl(input: $input) {
      PK
      SK
      CreatedAt
      UpdatedAt
      ExpiresAfter
      CallId
      CustomerPhoneNumber
      SystemPhoneNumber
      Status
      RecordingUrl
      PcaUrl
      TotalConversationDurationMillis
      AgentId
      Metadatajson
      CallCategories
      IssuesDetected
      CallSummaryText
      Owner
      SharedWith
    }
  }
`;

export const updateAgent = /* GraphQL */ `
  mutation UpdateAgent($input: UpdateAgentInput!) {
    updateAgent(input: $input) {
      PK
      SK
      CreatedAt
      UpdatedAt
      ExpiresAfter
      CallId
      CustomerPhoneNumber
      SystemPhoneNumber
      Status
      RecordingUrl
      PcaUrl
      TotalConversationDurationMillis
      AgentId
      Metadatajson
      CallCategories
      IssuesDetected
      CallSummaryText
      Owner
      SharedWith
    }
  }
`;

export const addCallCategory = /* GraphQL */ `
  mutation AddCallCategory($input: AddCallCategoryInput!) {
    addCallCategory(input: $input) {
      PK
      SK
      CreatedAt
      UpdatedAt
      ExpiresAfter
      CallId
      CustomerPhoneNumber
      SystemPhoneNumber
      Status
      RecordingUrl
      PcaUrl
      TotalConversationDurationMillis
      AgentId
      Metadatajson
      CallCategories
      IssuesDetected
      CallSummaryText
      Owner
      SharedWith
    }
  }
`;

export const addIssuesDetected = /* GraphQL */ `
  mutation AddIssuesDetected($input: AddIssuesDetectedInput!) {
    addIssuesDetected(input: $input) {
      PK
      SK
      CreatedAt
      UpdatedAt
      ExpiresAfter
      CallId
      CustomerPhoneNumber
      SystemPhoneNumber
      Status
      RecordingUrl
      PcaUrl
      TotalConversationDurationMillis
      AgentId
      Metadatajson
      CallCategories
      IssuesDetected
      CallSummaryText
      Owner
      SharedWith
    }
  }
`;

export const addCallSummaryText = /* GraphQL */ `
  mutation AddCallSummaryText($input: AddCallSummaryTextInput!) {
    addCallSummaryText(input: $input) {
      PK
      SK
      CreatedAt
      UpdatedAt
      ExpiresAfter
      CallId
      CustomerPhoneNumber
      SystemPhoneNumber
      Status
      RecordingUrl
      PcaUrl
      TotalConversationDurationMillis
      AgentId
      Metadatajson
      CallCategories
      IssuesDetected
      CallSummaryText
      Owner
      SharedWith
    }
  }
`;

export const addTranscriptSegment = /* GraphQL */ `
  mutation AddTranscriptSegment($input: AddTranscriptSegmentInput!) {
    addTranscriptSegment(input: $input) {
      PK
      SK
      CreatedAt
      UpdatedAt
      ExpiresAfter
      CallId
      SegmentId
      StartTime
      EndTime
      Transcript
      IsPartial
      Channel
      Speaker
      Owner
      SharedWith
    }
  }
`;

export const deleteCall = /* GraphQL */ `
  mutation DeleteCall($input: DeleteCallInput!) {
    deleteCall(input: $input) {
      CallId
      Owner
      SharedWith
    }
  }
`;

export const shareCall = /* GraphQL */ `
  mutation ShareCall($input: ShareCallInput!) {
    shareCall(input: $input) {
      CallId
      Owner
      SharedWith
    }
  }
`;

export const unshareCall = /* GraphQL */ `
  mutation UnshareCall($input: UnshareCallInput!) {
    unshareCall(input: $input) {
      CallId
      SharedWith
    }
  }
`;

export const deleteTranscriptSegment = /* GraphQL */ `
  mutation DeleteTranscriptSegment($input: DeleteTranscriptSegmentInput!) {
    deleteTranscriptSegment(input: $input) {
      CallId
    }
  }
`;

export const shareTranscriptSegment = /* GraphQL */ `
  mutation ShareTranscriptSegment($input: ShareTranscriptSegmentInput!) {
    shareTranscriptSegment(input: $input) {
      PK
    }
  }
`;

export const shareMeetings = /* GraphQL */ `
  mutation ShareMeetings($input: ShareMeetingsInput!) {
    shareMeetings(input: $input) {
      Calls
      Result
      Owner
      SharedWith
    }
  }
`;

export const deleteMeetings = /* GraphQL */ `
  mutation DeleteMeetings($input: DeleteMeetingsInput!) {
    deleteMeetings(input: $input) {
      Result
    }
  }
`;

export const createVirtualParticipant = /* GraphQL */ `
  mutation CreateVirtualParticipant($input: CreateVirtualParticipantInput!) {
    createVirtualParticipant(input: $input) {
      id
      meetingName
      meetingPlatform
      meetingId
      meetingPassword
      meetingTime
      scheduledFor
      isScheduled
      scheduleId
      status
      owner
      Owner
      SharedWith
      createdAt
      updatedAt
      CallId
      vncEndpoint
      vncPort
      vncReady
      taskPrivateIp
      vncPassword
      manualActionType
      manualActionMessage
      manualActionTimeoutSeconds
      manualActionStartTime
    }
  }
`;

export const updateVirtualParticipant = /* GraphQL */ `
  mutation UpdateVirtualParticipant($input: UpdateVirtualParticipantInput!) {
    updateVirtualParticipant(input: $input) {
      id
      meetingName
      meetingPlatform
      meetingId
      meetingPassword
      meetingTime
      scheduledFor
      isScheduled
      scheduleId
      status
      owner
      Owner
      SharedWith
      createdAt
      updatedAt
      CallId
      vncEndpoint
      vncPort
      vncReady
      taskPrivateIp
      vncPassword
      manualActionType
      manualActionMessage
      manualActionTimeoutSeconds
      manualActionStartTime
    }
  }
`;

export const endVirtualParticipant = /* GraphQL */ `
  mutation EndVirtualParticipant($input: EndVirtualParticipantInput!) {
    endVirtualParticipant(input: $input) {
      id
      status
      updatedAt
    }
  }
`;

export const shareVirtualParticipant = /* GraphQL */ `
  mutation ShareVirtualParticipant($input: ShareVirtualParticipantInput!) {
    shareVirtualParticipant(input: $input) {
      id
      Owner
      SharedWith
    }
  }
`;

export const unshareVirtualParticipant = /* GraphQL */ `
  mutation UnshareVirtualParticipant($input: UnshareVirtualParticipantInput!) {
    unshareVirtualParticipant(input: $input) {
      id
      SharedWith
    }
  }
`;

export const sendChatMessage = /* GraphQL */ `
  mutation SendChatMessage($input: SendChatMessageInput!) {
    sendChatMessage(input: $input) {
      MessageId
      Status
      CallId
      Response
    }
  }
`;

export const addChatToken = /* GraphQL */ `
  mutation AddChatToken($input: AddChatTokenInput!) {
    addChatToken(input: $input) {
      CallId
      MessageId
      Token
      IsComplete
      Sequence
      Timestamp
    }
  }
`;

export const updateChatButtonConfig = /* GraphQL */ `
  mutation UpdateChatButtonConfig($input: UpdateChatButtonConfigInput!) {
    updateChatButtonConfig(input: $input) {
      ChatButtonConfigId
      Success
    }
  }
`;

export const toggleVNCPreview = /* GraphQL */ `
  mutation ToggleVNCPreview($input: ToggleVNCPreviewInput!) {
    toggleVNCPreview(input: $input) {
      CallId
      Action
      Timestamp
      Success
      RequestedBy
    }
  }
`;

export const installMCPServer = /* GraphQL */ `
  mutation InstallMCPServer($input: InstallMCPServerInput!) {
    installMCPServer(input: $input) {
      ServerId
      Success
      Message
      BuildId
    }
  }
`;

export const uninstallMCPServer = /* GraphQL */ `
  mutation UninstallMCPServer($serverId: ID!) {
    uninstallMCPServer(serverId: $serverId) {
      ServerId
      Success
      Message
    }
  }
`;

export const updateMCPServer = /* GraphQL */ `
  mutation UpdateMCPServer($input: UpdateMCPServerInput!) {
    updateMCPServer(input: $input) {
      ServerId
      Success
      Message
      BuildId
    }
  }
`;