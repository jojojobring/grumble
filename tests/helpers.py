import http.client
import json


def request(server, app, method, path, body=None, token="auto", headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    all_headers = dict(headers or {})
    if token == "auto":
        all_headers["X-Grumble-Token"] = app.token
    elif token:
        all_headers["X-Grumble-Token"] = token
    connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=all_headers)
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response.status, data
