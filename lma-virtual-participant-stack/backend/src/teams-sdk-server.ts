import * as http from 'http';
import * as fs from 'fs';
import * as path from 'path';
import { fileURLToPath } from 'url';
import { AddressInfo } from 'net';

const distDir = path.dirname(fileURLToPath(import.meta.url));

const CONTENT_TYPES: Record<string, string> = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.mjs': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.wasm': 'application/wasm',
    '.map': 'application/json; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.woff': 'font/woff',
    '.woff2': 'font/woff2',
};

function contentTypeFor(filePath: string): string {
    return CONTENT_TYPES[path.extname(filePath).toLowerCase()] || 'application/octet-stream';
}

const PAGE_HTML = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>LMA Teams</title>
  <style>html,body{width:100%;height:100%;margin:0;background:#0b0b0b;}#lma-root{width:100vw;height:100vh;}</style>
</head>
<body>
  <div id="lma-root"></div>
  <script src="/teams-sdk-page.bundle.js"></script>
</body>
</html>`;

export interface TeamsSdkServerHandle {
    origin: string;
    close: () => Promise<void>;
}

export async function startTeamsSdkServer(): Promise<TeamsSdkServerHandle> {
    const server = http.createServer((req, res) => {
        const rawPath = (req.url || '/').split('?')[0];
        const urlPath = decodeURIComponent(rawPath);

        if (urlPath === '/' || urlPath === '/index.html') {
            res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
            res.end(PAGE_HTML);
            return;
        }

        const candidate = path.normalize(path.join(distDir, urlPath));
        if (candidate !== distDir && !candidate.startsWith(distDir + path.sep)) {
            res.writeHead(403);
            res.end('forbidden');
            return;
        }
        fs.readFile(candidate, (err, data) => {
            if (err) {
                res.writeHead(404);
                res.end('not found');
                return;
            }
            res.writeHead(200, { 'Content-Type': contentTypeFor(candidate) });
            res.end(data);
        });
    });

    await new Promise<void>((resolve, reject) => {
        server.once('error', reject);
        server.listen(0, '127.0.0.1', () => resolve());
    });

    const port = (server.address() as AddressInfo).port;
    // The ACS calling SDK's secure-context check accepts the literal hostname
    // "localhost" but rejects "127.0.0.1"; the listener stays bound to loopback.
    const origin = `http://localhost:${port}`;
    console.log(`[teams-sdk] embed server listening on ${origin}`);

    return {
        origin,
        close: () =>
            new Promise<void>((resolve) => {
                server.close(() => resolve());
            }),
    };
}
