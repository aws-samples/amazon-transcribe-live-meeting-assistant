The WebSocket client utility makes it easier to test LMA with a meeting recording. It reads a stereo WAV
file and streams its audio to the LMA WebSocket server, as described in
[the WebSocket streaming API](../../docs/websocket-streaming-api.md).

## How to use
The websocket client can be run from a local command line (such MacOs terminal) or from Cloud9/Cloud Shell command line. 
This command line environmentshould have access to AWS resources. 

To get started, cd to the `utilities/websocket-client` directory of the LMA repository.

1. Setup the package dependencies (first time you run the utility)

    `npm run setup`

2. Build and check for build errors (optional, but recommended whenever you make changes)

    `npm run build`

3.  Update following environment variables (export them or update them in .env file)

    `SAMPLE_RATE=8000`

    `BYTES_PER_SAMPLE=2`

    `CHUNK_SIZE_IN_MS=200`

    `REMOTE_SPEAKER_NAME='Other Participant'` (speaker name for the meeting audio, the WAV file's left channel)

    `LOCAL_SPEAKER_NAME='me@example.com'` (speaker name for the microphone, the right channel)

    `CALL_FROM_NUMBER` and `AGENT_ID`, the older names of these two variables, are still read.

    `LMA_ACCESS_JWT_TOKEN=`
    
    `LMA_ID_JWT_TOKEN=`

    `LMA_REFRESH_JWT_TOKEN=`

    Update LMA_ACCESS_JWT_TOKEN, LMA_ID_JWT_TOKEN and LMA_REFRESH_JWT_TOKEN with the 
    access token, identity token and refresh token respectively from the LMA 
    user authenticted session.

4. Run the websocket client using npm run command 

    `npm run start -- --uri <<Websocket Server Endpoint>>  --wavfile <<wav file name>>`

        `Websocket Server Endpoint` - obtain this from the "Outputs" section of the LMA stack in the CloudFormation console.
