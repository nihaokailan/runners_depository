from app import parse_multipart
body = b'--foo\r\nContent-Disposition: form-data; name="contact_number"\r\n\r\n09171234567\r\n--foo--\r\n'
fields, files = parse_multipart(body, b'foo')
print(fields)
print(files)
