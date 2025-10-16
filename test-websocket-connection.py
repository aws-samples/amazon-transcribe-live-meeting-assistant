#!/usr/bin/env python3
"""
Test WebSocket connection to API Gateway
"""
import asyncio
import websockets
import sys
import json

async def test_websocket(url, vp_id):
    """Test WebSocket connection to API Gateway"""
    
    # Add vpId as query parameter
    ws_url = f"{url}?vpId={vp_id}"
    
    print(f"=== Testing WebSocket Connection ===")
    print(f"URL: {ws_url}")
    print(f"VP ID: {vp_id}")
    print("")
    
    try:
        print("Attempting to connect...")
        async with websockets.connect(ws_url) as websocket:
            print("✓ WebSocket connected successfully!")
            print(f"Connection state: {websocket.state.name}")
            
            # Try to send a test message
            print("\nSending test message...")
            test_message = json.dumps({"action": "test", "data": "hello"})
            await websocket.send(test_message)
            print("✓ Message sent")
            
            # Wait for response (with timeout)
            try:
                response = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                print(f"✓ Received response: {response}")
            except asyncio.TimeoutError:
                print("⚠ No response received (timeout after 5s)")
            
            print("\n✓ WebSocket test completed successfully")
            
    except websockets.exceptions.InvalidStatusCode as e:
        print(f"✗ Connection failed with HTTP status: {e.status_code}")
        print(f"Headers: {e.headers}")
    except websockets.exceptions.WebSocketException as e:
        print(f"✗ WebSocket error: {e}")
    except Exception as e:
        print(f"✗ Unexpected error: {type(e).__name__}: {e}")
    
    print("\n=== Test Complete ===")

if __name__ == "__main__":
    # Default values
    api_id = "wsdj4af28j"
    region = "us-east-1"
    vp_id = sys.argv[1] if len(sys.argv) > 1 else "test-vp-id"
    
    ws_url = f"wss://{api_id}.execute-api.{region}.amazonaws.com/prod"
    
    # Run the test
    asyncio.run(test_websocket(ws_url, vp_id))
