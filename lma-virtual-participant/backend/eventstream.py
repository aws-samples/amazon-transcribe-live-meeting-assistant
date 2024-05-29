# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import struct
import binascii
import json

def decode_event(message):
    # Extract the prelude, headers, payload and CRC
    prelude = message[:8]
    total_length, headers_length = struct.unpack('>II', prelude)
    prelude_crc = struct.unpack('>I', message[8:12])[0]
    headers = message[12:12+headers_length]
    payload = message[12+headers_length:-4]
    message_crc = struct.unpack('>I', message[-4:])[0]

    # Check the CRCs
    assert prelude_crc == binascii.crc32(prelude) & 0xffffffff, "Prelude CRC check failed"
    assert message_crc == binascii.crc32(message[:-4]) & 0xffffffff, "Message CRC check failed"

    # Parse the headers
    headers_dict = {}
    while headers:
        name_len = headers[0]
        name = headers[1:1+name_len].decode('utf-8')
        value_type = headers[1+name_len]
        value_len = struct.unpack('>H', headers[2+name_len:4+name_len])[0]
        value = headers[4+name_len:4+name_len+value_len].decode('utf-8')
        headers_dict[name] = value
        headers = headers[4+name_len+value_len:]

    return headers_dict, json.loads(payload)

def create_audio_event(payload):
    #Build our headers
    #ContentType
    contentTypeHeader = get_headers(":content-type", "application/octet-stream")
    eventTypeHeader = get_headers(":event-type", "AudioEvent")
    messageTypeHeader = get_headers(":message-type", "event")
    headers = []
    headers.extend(contentTypeHeader)
    headers.extend(eventTypeHeader)
    headers.extend(messageTypeHeader)

    #Calculate total byte length and headers byte length
    totalByteLength = struct.pack('>I', len(headers) + len(payload) + 16) #16 accounts for 8 byte prelude, 2x 4 byte crcs.
    headersByteLength = struct.pack('>I', len(headers))

    #Build the prelude
    prelude = bytearray([0] * 8)
    prelude[:4] = totalByteLength
    prelude[4:] = headersByteLength

    #calculate checksum for prelude (total + headers)
    preludeCRC = struct.pack('>I', binascii.crc32(prelude) & 0xffffffff)

    #Construct the message
    messageAsList = bytearray()
    messageAsList.extend(prelude)
    messageAsList.extend(preludeCRC)
    messageAsList.extend(headers)
    messageAsList.extend(payload)

    #Calculate checksum for message
    message = bytes(messageAsList)
    messageCRC = struct.pack('>I', binascii.crc32(message) & 0xffffffff)

    #Add message checksum
    messageAsList.extend(messageCRC)
    message = bytes(messageAsList)

    return message

def get_headers(headerName, headerValue):
    name = headerName.encode('utf-8')
    nameByteLength = bytes([len(name)])
    valueType = bytes([7]) #7 represents a string
    value = headerValue.encode('utf-8')
    valueByteLength = struct.pack('>H', len(value))

    #Construct the header
    headerList = bytearray()
    headerList.extend(nameByteLength)
    headerList.extend(name)
    headerList.extend(valueType)
    headerList.extend(valueByteLength)
    headerList.extend(value)

    return headerList