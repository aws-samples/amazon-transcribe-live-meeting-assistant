import { Item, Result, TranscriptEvent } from '@aws-sdk/client-transcribe-streaming';

export const concatItemsIntoTranscript = (items: Item[]) => {
    let text = '';
    items.forEach(item => {
        if (item.Type === 'punctuation') {
            text = text.trim();
        }
        text += item.Content + ' ';
    });

    // Trim last space
    text = text.trim();
    return text;
};

export const splitTranscriptEventBySpeaker = (transcript:TranscriptEvent):TranscriptEvent[] => {
    const itemsBySpeaker:{[key: string]: Item[]} = {};

    let initialSpeaker:string;
    let lastSpeaker:string;
    let firstResult:Result;
    if(transcript.Transcript && 
        transcript.Transcript.Results &&
        transcript.Transcript.Results[0] &&
        transcript.Transcript.Results[0].Alternatives && 
        transcript.Transcript.Results[0].Alternatives[0] &&
        transcript.Transcript.Results[0].Alternatives[0].Items) {
        
        firstResult = transcript.Transcript.Results[0];
        if (firstResult.IsPartial) {
            return [transcript]; // we don't split here because partials dont contain speaker information
        }
        
        transcript.Transcript.Results[0].Alternatives[0].Items.forEach(item => {
            if (item.Speaker) { // this is because punctuation does not have a speaker label.
                lastSpeaker = item.Speaker; 
                if (initialSpeaker === undefined) {
                    initialSpeaker = item.Speaker;
                }
            }
            if (lastSpeaker) {
                if(!itemsBySpeaker[lastSpeaker]) {
                    itemsBySpeaker[lastSpeaker] = [];  
                }
                itemsBySpeaker[lastSpeaker].push(item);
            }
        });
    }

    return Object.keys(itemsBySpeaker).map(speaker => {
        return {
            Transcript: {
                Results: [{
                    Alternatives: [{
                        Items: itemsBySpeaker[speaker],
                        Transcript: concatItemsIntoTranscript(itemsBySpeaker[speaker])
                    }],
                    ChannelId: firstResult?.ChannelId,
                    EndTime: itemsBySpeaker[speaker][itemsBySpeaker[speaker].length -1].EndTime,
                    IsPartial: firstResult?.IsPartial,
                    ResultId: firstResult?.ResultId + (speaker === initialSpeaker ? '' : '-' + speaker),
                    StartTime: itemsBySpeaker[speaker][0].StartTime  
                }]
            }
        };
    });
};
