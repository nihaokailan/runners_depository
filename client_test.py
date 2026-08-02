import http.client

boundary = '----WebKitFormBoundary7MA4YWxkTrZu0gW'
body_lines = []
fields = {
    'first_name': 'Juan',
    'middle_name': 'Santos',
    'surname': 'Dela Cruz',
    'payment_mode': 'GCash',
    'payment_date': '2026-08-02',
    'email': 'juan@gmail.com',
    'contact_number': '09171234567'
}
for name, value in fields.items():
    body_lines.append('--' + boundary)
    body_lines.append(f'Content-Disposition: form-data; name="{name}"')
    body_lines.append('')
    body_lines.append(value)
body_lines.append('--' + boundary + '--')
body_lines.append('')
body = '\r\n'.join(body_lines).encode('utf-8')
conn = http.client.HTTPConnection('localhost', 8000)
headers = {
    'Content-Type': f'multipart/form-data; boundary={boundary}',
    'Content-Length': str(len(body))
}
conn.request('POST', '/register', body, headers)
resp = conn.getresponse()
print(resp.status, resp.reason)
print(resp.read().decode('utf-8', 'ignore'))
