const http = require("node:http");
const { port } = require("../config");

function startHealthServer(client) {
  const server = http.createServer((request, response) => {
    if (request.url === "/healthz") {
      const body = JSON.stringify({
        ok: true,
        discordReady: Boolean(client.isReady?.()),
        uptimeSeconds: Math.floor(process.uptime()),
        timestamp: new Date().toISOString()
      });

      response.writeHead(200, {
        "content-type": "application/json; charset=utf-8"
      });
      response.end(body);
      return;
    }

    response.writeHead(200, {
      "content-type": "text/plain; charset=utf-8"
    });
    response.end("MotionXBot is running.\n");
  });

  server.listen(port, "0.0.0.0", () => {
    console.log(`Health server listening on port ${port}`);
  });

  return server;
}

module.exports = {
  startHealthServer
};
