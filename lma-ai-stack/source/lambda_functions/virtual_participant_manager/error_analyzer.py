# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""
Virtual Participant Error Analyzer
Provides intelligent error categorization and troubleshooting recommendations
"""

import re
from typing import Dict, List, Optional, Tuple
from enum import Enum

class ErrorCategory(Enum):
    AUTHENTICATION_ERROR = "AUTHENTICATION_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"
    MEETING_NOT_FOUND = "MEETING_NOT_FOUND"
    MEETING_ENDED = "MEETING_ENDED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    PLATFORM_ERROR = "PLATFORM_ERROR"
    TIMEOUT_ERROR = "TIMEOUT_ERROR"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"

class ErrorAnalyzer:
    """
    Analyzes error messages and provides categorization and troubleshooting steps
    """
    
    def __init__(self):
        self.error_patterns = {
            ErrorCategory.AUTHENTICATION_ERROR: [
                r"authentication.*failed",
                r"invalid.*credentials",
                r"unauthorized",
                r"access.*denied",
                r"login.*failed",
                r"auth.*error"
            ],
            ErrorCategory.NETWORK_ERROR: [
                r"network.*error",
                r"connection.*failed",
                r"timeout",
                r"dns.*resolution.*failed",
                r"host.*unreachable",
                r"socket.*error",
                r"connection.*refused"
            ],
            ErrorCategory.MEETING_NOT_FOUND: [
                r"meeting.*not.*found",
                r"invalid.*meeting.*id",
                r"meeting.*does.*not.*exist",
                r"room.*not.*found",
                r"conference.*not.*found"
            ],
            ErrorCategory.MEETING_ENDED: [
                r"meeting.*ended",
                r"meeting.*has.*ended",
                r"conference.*terminated",
                r"session.*expired",
                r"meeting.*closed"
            ],
            ErrorCategory.PERMISSION_DENIED: [
                r"permission.*denied",
                r"not.*authorized",
                r"access.*forbidden",
                r"insufficient.*privileges",
                r"not.*allowed.*to.*join"
            ],
            ErrorCategory.INVALID_CREDENTIALS: [
                r"invalid.*password",
                r"incorrect.*password",
                r"wrong.*credentials",
                r"password.*required",
                r"meeting.*password.*incorrect"
            ],
            ErrorCategory.PLATFORM_ERROR: [
                r"zoom.*error",
                r"teams.*error",
                r"webex.*error",
                r"platform.*error",
                r"service.*unavailable",
                r"internal.*server.*error"
            ],
            ErrorCategory.TIMEOUT_ERROR: [
                r"timeout",
                r"request.*timed.*out",
                r"connection.*timeout",
                r"operation.*timeout"
            ]
        }
        
        self.troubleshooting_steps = {
            ErrorCategory.AUTHENTICATION_ERROR: [
                "Verify that the meeting credentials are correct",
                "Check if the meeting requires a password",
                "Ensure the user has permission to join this meeting",
                "Try refreshing the authentication token"
            ],
            ErrorCategory.NETWORK_ERROR: [
                "Check internet connectivity",
                "Verify firewall settings allow meeting platform access",
                "Try connecting from a different network",
                "Check if the meeting platform is experiencing outages"
            ],
            ErrorCategory.MEETING_NOT_FOUND: [
                "Verify the meeting ID is correct",
                "Check if the meeting has been scheduled",
                "Ensure the meeting hasn't been cancelled",
                "Confirm the meeting platform is correct (Zoom, Teams, etc.)"
            ],
            ErrorCategory.MEETING_ENDED: [
                "Check if the meeting is still active",
                "Verify the meeting end time",
                "Contact the meeting organizer to restart if needed",
                "Wait for the next scheduled occurrence if it's a recurring meeting"
            ],
            ErrorCategory.PERMISSION_DENIED: [
                "Contact the meeting organizer for access",
                "Check if registration is required",
                "Verify you're using the correct meeting link",
                "Ensure the meeting allows external participants"
            ],
            ErrorCategory.INVALID_CREDENTIALS: [
                "Verify the meeting password is correct",
                "Check for any special characters in the password",
                "Contact the meeting organizer for the correct password",
                "Try joining without a password if it's not required"
            ],
            ErrorCategory.PLATFORM_ERROR: [
                "Check the meeting platform status page",
                "Try again in a few minutes",
                "Contact the meeting platform support",
                "Use an alternative meeting platform if available"
            ],
            ErrorCategory.TIMEOUT_ERROR: [
                "Check internet connection stability",
                "Try again with a more stable network",
                "Increase timeout settings if configurable",
                "Contact support if the issue persists"
            ],
            ErrorCategory.UNKNOWN_ERROR: [
                "Check the meeting platform status",
                "Verify all meeting details are correct",
                "Try joining manually to test connectivity",
                "Contact support with the specific error details"
            ]
        }
    
    def analyze_error(self, error_message: str, error_code: str = None, 
                     platform: str = None) -> Tuple[ErrorCategory, List[str]]:
        """
        Analyze an error message and return category and troubleshooting steps
        
        Args:
            error_message: The error message to analyze
            error_code: Optional error code
            platform: Optional platform name (zoom, teams, etc.)
            
        Returns:
            Tuple of (ErrorCategory, List of troubleshooting steps)
        """
        
        if not error_message:
            return ErrorCategory.UNKNOWN_ERROR, self.troubleshooting_steps[ErrorCategory.UNKNOWN_ERROR]
        
        error_message_lower = error_message.lower()
        
        # Check each category's patterns
        for category, patterns in self.error_patterns.items():
            for pattern in patterns:
                if re.search(pattern, error_message_lower, re.IGNORECASE):
                    return category, self.troubleshooting_steps[category]
        
        # If no pattern matches, return unknown error
        return ErrorCategory.UNKNOWN_ERROR, self.troubleshooting_steps[ErrorCategory.UNKNOWN_ERROR]
    
    def get_error_severity(self, category: ErrorCategory) -> str:
        """
        Get the severity level of an error category
        
        Args:
            category: The error category
            
        Returns:
            Severity level: 'low', 'medium', 'high', 'critical'
        """
        
        severity_map = {
            ErrorCategory.AUTHENTICATION_ERROR: 'medium',
            ErrorCategory.NETWORK_ERROR: 'high',
            ErrorCategory.MEETING_NOT_FOUND: 'high',
            ErrorCategory.MEETING_ENDED: 'low',
            ErrorCategory.PERMISSION_DENIED: 'medium',
            ErrorCategory.INVALID_CREDENTIALS: 'medium',
            ErrorCategory.PLATFORM_ERROR: 'high',
            ErrorCategory.TIMEOUT_ERROR: 'medium',
            ErrorCategory.UNKNOWN_ERROR: 'high'
        }
        
        return severity_map.get(category, 'medium')
    
    def is_retryable_error(self, category: ErrorCategory) -> bool:
        """
        Determine if an error category is typically retryable
        
        Args:
            category: The error category
            
        Returns:
            True if the error is typically retryable
        """
        
        retryable_categories = {
            ErrorCategory.NETWORK_ERROR,
            ErrorCategory.TIMEOUT_ERROR,
            ErrorCategory.PLATFORM_ERROR
        }
        
        return category in retryable_categories
    
    def get_platform_specific_guidance(self, platform: str, category: ErrorCategory) -> List[str]:
        """
        Get platform-specific troubleshooting guidance
        
        Args:
            platform: The meeting platform (zoom, teams, webex, etc.)
            category: The error category
            
        Returns:
            List of platform-specific troubleshooting steps
        """
        
        platform_guidance = {
            'zoom': {
                ErrorCategory.AUTHENTICATION_ERROR: [
                    "Check Zoom account permissions",
                    "Verify Zoom API credentials",
                    "Ensure Zoom account is not suspended"
                ],
                ErrorCategory.MEETING_NOT_FOUND: [
                    "Verify the Zoom meeting ID format (9-11 digits)",
                    "Check if the meeting is a personal meeting room",
                    "Ensure the meeting hasn't been deleted from Zoom"
                ],
                ErrorCategory.INVALID_CREDENTIALS: [
                    "Check if the meeting requires a passcode",
                    "Verify the meeting passcode format",
                    "Try using the meeting URL instead of ID + passcode"
                ]
            },
            'teams': {
                ErrorCategory.AUTHENTICATION_ERROR: [
                    "Check Microsoft Teams account status",
                    "Verify Azure AD permissions",
                    "Ensure Teams license is active"
                ],
                ErrorCategory.MEETING_NOT_FOUND: [
                    "Verify the Teams meeting URL is complete",
                    "Check if the meeting is in the correct tenant",
                    "Ensure the meeting hasn't been cancelled in Outlook"
                ]
            },
            'webex': {
                ErrorCategory.AUTHENTICATION_ERROR: [
                    "Check Webex account permissions",
                    "Verify Webex site URL is correct",
                    "Ensure Webex account is not locked"
                ],
                ErrorCategory.MEETING_NOT_FOUND: [
                    "Verify the Webex meeting number",
                    "Check if the meeting is scheduled for the correct time zone",
                    "Ensure the meeting hasn't been moved or cancelled"
                ]
            }
        }
        
        platform_lower = platform.lower() if platform else ''
        
        # Get platform-specific guidance if available
        if platform_lower in platform_guidance:
            platform_steps = platform_guidance[platform_lower].get(category, [])
            if platform_steps:
                return platform_steps
        
        # Fall back to general troubleshooting steps
        return self.troubleshooting_steps.get(category, [])
    
    def create_error_report(self, error_message: str, error_code: str = None, 
                          platform: str = None, context: Dict = None) -> Dict:
        """
        Create a comprehensive error report
        
        Args:
            error_message: The error message
            error_code: Optional error code
            platform: Optional platform name
            context: Optional additional context
            
        Returns:
            Comprehensive error report dictionary
        """
        
        category, general_steps = self.analyze_error(error_message, error_code, platform)
        platform_steps = self.get_platform_specific_guidance(platform, category)
        severity = self.get_error_severity(category)
        is_retryable = self.is_retryable_error(category)
        
        return {
            'errorMessage': error_message,
            'errorCode': error_code,
            'errorCategory': category.value,
            'severity': severity,
            'isRetryable': is_retryable,
            'troubleshootingSteps': platform_steps if platform_steps != general_steps else general_steps,
            'generalSteps': general_steps,
            'platformSpecificSteps': platform_steps if platform_steps != general_steps else [],
            'platform': platform,
            'context': context or {}
        }
