"""
Virtual Participant Status Manager

Simple status update functionality for Virtual Participants.
Integrates with Amplify GraphQL API to update VP status.
"""

import os
import json
import boto3
import logging
from typing import Optional
from botocore.exceptions import ClientError
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
import requests

logger = logging.getLogger(__name__)

class VirtualParticipantStatusManager:
    """Manages status updates for Virtual Participants"""
    
    def __init__(self, participant_id: str):
        self.participant_id = participant_id
        self.graphql_endpoint = os.environ.get('GRAPHQL_ENDPOINT')
        self.aws_region = os.environ.get('AWS_REGION', 'us-east-1')
        
        if not self.graphql_endpoint:
            raise ValueError("GRAPHQL_ENDPOINT environment variable is required")
        
        # Set up AWS credentials for signing requests
        self.session = boto3.Session()
        self.credentials = self.session.get_credentials()
    
    def _sign_request(self, request):
        """Sign the request with AWS SigV4"""
        SigV4Auth(self.credentials, 'appsync', self.aws_region).add_auth(request)
    
    def update_status(self, status: str, error_message: Optional[str] = None) -> bool:
        """
        Update the status of the Virtual Participant
        
        Args:
            status: One of 'JOINING', 'COMPLETED', 'FAILED'
            error_message: Optional error message for failed status
            
        Returns:
            bool: True if update was successful, False otherwise
        """
        try:
            mutation = """
            mutation UpdateVirtualParticipant($input: UpdateVirtualParticipantInput!) {
                updateVirtualParticipant(input: $input) {
                    id
                    status
                    updatedAt
                }
            }
            """
            
            variables = {
                "input": {
                    "id": self.participant_id,
                    "status": status
                }
            }
            
            if status == "FAILED" and error_message:
                logger.error(f"VP {self.participant_id} failed: {error_message}")
            
            payload = {
                "query": mutation,
                "variables": variables
            }
            
            request = AWSRequest(
                method='POST',
                url=self.graphql_endpoint,
                data=json.dumps(payload),
                headers={'Content-Type': 'application/json'}
            )
            self._sign_request(request)
            
            response = requests.post(
                request.url,
                data=request.body,
                headers=dict(request.headers)
            )
            
            if response.status_code == 200:
                response_data = response.json()
                if 'errors' in response_data:
                    logger.error(f"GraphQL errors: {response_data['errors']}")
                    return False
                return True
            else:
                logger.error(f"Failed to update VP status: HTTP {response.status_code}")
                return False
            
        except Exception as e:
            logger.error(f"Unexpected error updating VP status: {e}")
            return False
    
    def set_joining(self) -> bool:
        """Set status to JOINING - VP is attempting to join meeting"""
        return self.update_status("JOINING")
    
    def set_joined(self) -> bool:
        """Set status to JOINED - VP successfully joined meeting"""
        result = self.update_status("JOINED")
        if not result:
            raise Exception("Failed to update VP status to JOINED via GraphQL")
        return result
    
    def set_completed(self) -> bool:
        """Set status to COMPLETED - Meeting ended successfully"""
        return self.update_status("COMPLETED")
    
    def set_failed(self, error_message: Optional[str] = None) -> bool:
        """Set status to FAILED - Wrong password, invalid meeting ID, or other error"""
        return self.update_status("FAILED", error_message)


# Convenience functions for easy integration
def create_status_manager(participant_id: str) -> VirtualParticipantStatusManager:
    """Create a new status manager instance"""
    return VirtualParticipantStatusManager(participant_id)

def update_participant_status(participant_id: str, status: str, error_message: Optional[str] = None) -> bool:
    """Quick function to update participant status"""
    manager = create_status_manager(participant_id)
    return manager.update_status(status, error_message)
