import { FastifyRequest, FastifyReply } from 'fastify';
import { CognitoJwtVerifier } from 'aws-jwt-verify';
import dotenv from 'dotenv';
dotenv.config();

const USERPOOL_ID = process.env['USERPOOL_ID'] || '';
const cognitoJwtVerifier = CognitoJwtVerifier.create({
    userPoolId: USERPOOL_ID,
});

type queryobj = {
    authorization: string
};

type headersobj = {
    authorization: string
};

export const jwtVerifier = async (request: FastifyRequest, reply: FastifyReply) => {
    const query = request.query as queryobj;
    const headers = request.headers as headersobj;
    const auth = query.authorization || headers.authorization;

    if (!auth) {
        request.log.error('No authorization query string or header found');
        return reply.status(401).send();
    }

    const match = auth?.match(/^Bearer (.+)$/);
    if (!match) {
        request.log.error('No Bearer token found in header or query string');
        return reply.status(401).send();
    }

    const accessToken = match[1];
    try {
        const payload = await cognitoJwtVerifier.verify(accessToken, { clientId: null, tokenUse: 'access' });      
        if (!payload) {
            request.log.error('Connection not authorized. Returning 401');
            return reply.status(401).send();
        }
        request.log.info(`Connection request authorized at ${payload.auth_time}`);
        return;
    } catch (err) {
        request.log.error(`Error Authorizing client connection ${err}`);
        return reply.status(401).send();
    }
};



