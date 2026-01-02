#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""
Tool wrapper to capture thinking steps for UI display
"""

import logging
from typing import Callable, Any, Dict
from datetime import datetime
from functools import wraps

logger = logging.getLogger()

# Global counter for thinking step sequences
_thinking_sequence_counter = {}

def get_next_sequence(message_id: str) -> int:
    """Get next sequence number for a message"""
    if message_id not in _thinking_sequence_counter:
        _thinking_sequence_counter[message_id] = 0
    seq = _thinking_sequence_counter[message_id]
    _thinking_sequence_counter[message_id] += 1
    return seq

def wrap_tool_with_thinking(tool_func: Callable, tool_name: str, call_id: str, message_id: str, 
                            send_thinking_fn: Callable) -> Callable:
    """
    Wrap a tool function to send thinking steps before and after execution
    
    Args:
        tool_func: The original tool function
        tool_name: Name of the tool for display
        call_id: Call ID for AppSync
        message_id: Message ID for AppSync
        send_thinking_fn: Function to send thinking steps to AppSync
    
    Returns:
        Wrapped tool function that sends thinking steps
    """
    @wraps(tool_func)
    def wrapped_tool(*args, **kwargs):
        try:
            # Send tool_use thinking step
            sequence = get_next_sequence(message_id)
            thinking_step = {
                'type': 'tool_use',
                'tool_name': tool_name,
                'tool_input': {
                    'args': args,
                    'kwargs': kwargs
                },
                'timestamp': datetime.utcnow().isoformat()
            }
            logger.info(f"🔧 Tool invoked: {tool_name} with args={args}, kwargs={kwargs}")
            send_thinking_fn(call_id, message_id, thinking_step, sequence)
            
            # Execute the actual tool
            result = tool_func(*args, **kwargs)
            
            # Send tool_result thinking step
            sequence = get_next_sequence(message_id)
            thinking_step = {
                'type': 'tool_result',
                'tool_name': tool_name,
                'result': str(result)[:500] if result else '',  # Truncate long results
                'success': True,
                'timestamp': datetime.utcnow().isoformat()
            }
            logger.info(f"✓ Tool completed: {tool_name}")
            send_thinking_fn(call_id, message_id, thinking_step, sequence)
            
            return result
            
        except Exception as e:
            # Send error thinking step
            sequence = get_next_sequence(message_id)
            thinking_step = {
                'type': 'tool_result',
                'tool_name': tool_name,
                'result': f"Error: {str(e)}",
                'success': False,
                'timestamp': datetime.utcnow().isoformat()
            }
            logger.error(f"❌ Tool failed: {tool_name} - {str(e)}")
            send_thinking_fn(call_id, message_id, thinking_step, sequence)
            raise
    
    return wrapped_tool