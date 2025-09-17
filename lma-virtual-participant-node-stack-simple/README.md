# LMA Virtual Participant - Node.js Implementation

A modern Node.js/TypeScript implementation of the LMA Virtual Participant that replaces the Python version with enhanced capabilities.

## 🚀 **Key Features**

- **3 Meeting Platforms**: Chime, Zoom, and Webex (vs Python's 2)
- **Modern Architecture**: Node.js/TypeScript with Puppeteer
- **Complete LMA Integration**: Kinesis streaming, S3 recording, GraphQL status updates
- **Enhanced Reliability**: Better error handling and retry logic
- **Production Ready**: Integrated with LMA build and deployment system

## 📁 **Structure**

```
lma-virtual-participant-node-stack-simple/
├── template.yaml          # CloudFormation template (complete LMA integration)
└── backend/               # Node.js application
    ├── Dockerfile         # Production container
    ├── package.json       # Dependencies
    ├── tsconfig.json      # TypeScript configuration
    └── src/               # Application source code
        ├── index.ts       # Main application (meeting.py equivalent)
        ├── chime.ts       # Amazon Chime handler
        ├── zoom.ts        # Zoom handler
        ├── webex.ts       # Webex handler (NEW)
        ├── scribe.ts      # Transcription service
        ├── kinesis-stream.ts  # LMA Kinesis integration
        ├── recording.ts   # Audio recording and S3 upload
        ├── status-manager.ts  # GraphQL status management
        └── details.ts     # Configuration management
```

## 🏗️ **Deployment**

This stack is integrated with the main LMA build system. **No separate deployment needed.**

### **Build and Deploy with LMA:**
```bash
# From LMA root directory
./publish.sh <bucket-basename> <prefix> <region>
```

The Node.js Virtual Participant will be automatically:
1. **Packaged** with other LMA stacks
2. **Uploaded** to S3 artifacts bucket
3. **Deployed** as part of main LMA CloudFormation stack
4. **Integrated** with existing LMA infrastructure

## 🎯 **LMA Integration**

### **Replaces Python Virtual Participant:**
- Same CloudFormation interface
- Same Step Functions API
- Same environment variables
- Same Kinesis record formats
- Same S3 recording formats
- Same GraphQL status updates

### **Enhanced Capabilities:**
- **Webex Support**: Additional meeting platform
- **Better Error Handling**: More robust failure recovery
- **Modern Browser Automation**: Puppeteer with system Chromium
- **TypeScript**: Better type safety and maintainability

## 🔧 **Configuration**

All configuration is handled through CloudFormation parameters in the main LMA stack:

- **Meeting Platforms**: Chime, Zoom, Webex
- **Transcription**: Language, vocabulary, content redaction
- **Recording**: Audio recording enable/disable, S3 storage
- **Integration**: Kinesis streams, GraphQL endpoints, DynamoDB tables

## 📊 **Monitoring**

Monitor through standard LMA CloudWatch logs:
- **ECS Task Logs**: `/aws/ecs/{LMA_STACK_NAME}/virtual-participant-node`
- **Step Functions**: `{LMA_STACK_NAME}-LMAVirtualParticipantScheduler-Node`

## 🎉 **Migration from Python**

This Node.js implementation is a **drop-in replacement** for the Python Virtual Participant:

1. **Same API**: Identical Step Functions input/output
2. **Same Integration**: Compatible with all LMA services
3. **Enhanced Features**: Additional platform support and reliability
4. **No Changes Required**: Existing LMA workflows continue to work

## 📄 **License**

Same license as the main LMA project.

---

**Note**: This implementation provides all Python LMA Virtual Participant features plus enhanced capabilities, making it a superior replacement while maintaining full compatibility with the existing LMA ecosystem.
