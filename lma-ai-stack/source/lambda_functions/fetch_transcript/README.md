# Fetch Transcript Lambda Function

This Lambda function retrieves an entire call transcript as a string, in the format of:

`SPEAKER_LABEL: Transcript`

For example:

```
AGENT: Hello, thank you for calling. How can I help you?
CALLER: Hi, I'm calling about my rewards card.
```
There are new lines for each speaker turn. 

The Lambda function accepts 3 parameters in the event:

### CallId (string)
This is the callId of the call to get the transcript

### ProcessTranscript (bool) 
If processTranscript is set to true, the following processing will occur:

1. Sequential speaker transcript segments will be merged together into one utterance.
2. Filler words will be removed (uhh's and uhm's).
3. `AGENT_ASSIST` and other non `CALLER` or `AGENT` messages will be removed.
4. HTML elements in the transcript such as Issue tags will be removed.

### TokenCount (int)

This is the maximum number of words and symbols that will be returned. This can be used if your summarization model has a max token count.  If `0`, the entire transcript will be returned. 

## Example event payload:

```
{
  "CallId": "2483734-34343-asdfs-21334",
  "ProcessTranscript": True,
  "TokenCount": 1024
}
```
## Lambda Response

The Lambda function returns a json with a single parameter, called `transcript` that contains the entire transcript as a string.