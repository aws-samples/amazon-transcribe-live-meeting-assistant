import { emitKeypressEvents } from 'readline';
import { Command } from 'commander';
import { WebSocket } from 'ws';
import * as fs from 'fs';
import Chain from 'stream-chain';
import { randomUUID } from 'crypto';
import { CallMetaData } from '../../../lca-websocket-stack/source/app/src/lca';

import dotenv from 'dotenv';
dotenv.config();

const SAMPLE_RATE = parseInt(process.env['SAMPLE_RATE'] || '8000', 10);
const BYTES_PER_SAMPLE = parseInt(process.env['BYTES_PER_SAMPLE'] || '2', 10);
const CHUNK_SIZE_IN_MS = parseInt(process.env['CHUNK_SIZE_IN_MS'] || '200', 10);

type CmdOptions = {
    uri?: string;
    wavfile?: string;
};
const _CALL_ID = randomUUID();

const timer = (millisec: number) => new Promise(cb => setTimeout(cb, millisec));

new Command()
    .description('LCA Websocket client')
    .showHelpAfterError()
    .argument('[serveruri]', 'URI of websocket server')
    .option('--uri <uri>', 'URI of websocket server')
    .option('--wavfile <wavfile>', 'WAV file to stream')
    .action(async (serveruri: string | undefined, options: CmdOptions, command: Command): Promise<void> => {
        if(options.uri && serveruri) {
            command.error('More than one server URI specified!');
        }

        const uri = options.uri ?? serveruri;
        if (uri) {
            const jwtToken = process.env['LCA_JWT_TOKEN'] || undefined;

            const ws = new WebSocket(uri, {
                headers: {
                    authorization: `Bearer ${jwtToken}`
                }
            });

            ws.on('open', () => {
                console.log('Connected to server');

                console.log('Sending Call start event');

                (async () => {
                    const metadata: CallMetaData = {
                        callId: _CALL_ID,
                        fromNumber: process.env['CALL_FROM_NUMBER'] || '+9165551234',
                        toNumber: process.env['CALL_TO_NUMBER'] || '+8001112222',
                        agentId: process.env['AGENT_ID'] || 'websocket',
                        samplingRate: SAMPLE_RATE,
                        callEvent: 'START'
                    };
                    ws.send(JSON.stringify(metadata), {
                        binary: false
                    });
                    await timer(2000);
                })();

                const CHUNK_SIZE = SAMPLE_RATE * (CHUNK_SIZE_IN_MS/1000) * BYTES_PER_SAMPLE * 2;
				
                const audiopipeline:Chain = new Chain([
                    fs.createReadStream(options.wavfile as fs.PathLike, { highWaterMark: CHUNK_SIZE }),
                    async data => {
                        await timer(CHUNK_SIZE_IN_MS);
                        return data;
                    }
                ]);

                console.log('Sending the audio data chunks');
                (async () => {
                    for await (const chunk of audiopipeline) {
                        // console.log(`Sending chunk of size ${chunk.length}`);
                        ws.send(chunk, {
                            binary: true
                        });
                    }
                })().finally(()=>{
                    console.log('Finished Sending the audio data chunks');
                    console.log('Sending Call End Event');

                    const metadata: CallMetaData = {
                        callId: _CALL_ID,
                        fromNumber: process.env['CALL_FROM_NUMBER'] || '+9165551234',
                        toNumber: process.env['CALL_TO_NUMBER'] || '+8001112222',
                        agentId: process.env['AGENT_ID'] || 'websocket',
                        samplingRate: SAMPLE_RATE,
                        callEvent: 'END'
                    };
                    ws.send(JSON.stringify(metadata), {
                        binary: false
                    });
                });

            });
			
            ws.on('message', (message: string) => {
                console.log(`Received message from server: ${message}`);
            });
			
            ws.on('close', () => {
                console.log('Disconnected from server');
            });
        }
    })
    .parseAsync(process.argv);

emitKeypressEvents(process.stdin);
process.stdin.setRawMode(true);
    
let ctrlcHit = false;
process.stdin.on('keypress', (str, key) => {
    if(key.ctrl && (key.name === 'c' || key.name === 'd')) {
        if(!ctrlcHit) {
            closer();
            ctrlcHit = true;
        } else {
            console.log('Terminating now!');
            process.exit(1);    // If hit twice, exit immediately
        }
    } else {
        console.log(`You pressed the ${JSON.stringify(str)} key: ${JSON.stringify(key)}`);
    }
});
    
process.once('SIGTERM', () => {
    console.log('SIGTERM!');
    closer();
});

process.once('SIGINT', () => {
    console.log('SIGINT!');
    closer();
});


const closer = () => {
    console.log('Closing...');
    process.exit(1);
};