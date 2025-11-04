# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""
Virtual Participant Manager Lambda Function
Handles VP termination with registry-based task ARN lookup
"""

import os
import json
import boto3
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from ecs_manager import ECSTaskManager

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS clients
dynamodb = boto3.resource('dynamodb')
scheduler = boto3.client('scheduler')

class VirtualParticipantManager:
    def __init__(self, table_name: str):
        self.table = dynamodb.Table(table_name)
        self.ecs_manager = ECSTaskManager()
        
    def get_current_timestamp(self) -> str:
        """Get current timestamp in ISO format"""
        return datetime.now(timezone.utc).isoformat()
    
    def get_virtual_participant(self, vp_id: str) -> Optional[Dict[str, Any]]:
        """Get Virtual Participant by ID"""
        try:
            response = self.table.get_item(Key={'id': vp_id})
            return response.get('Item')
        except Exception as e:
            logger.error(f"Error getting VP {vp_id}: {str(e)}")
            return None
    
    def end_virtual_participant(self, vp_id: str, end_reason: str, ended_by: str = None) -> Dict[str, Any]:
        """End a Virtual Participant with ECS container termination and Kinesis END event"""
        
        current_time = self.get_current_timestamp()
        
        logger.info(f"Ending Virtual Participant {vp_id}")
        
        # Get VP details
        vp = self.get_virtual_participant(vp_id)
        if not vp:
            raise ValueError(f"Virtual Participant {vp_id} not found")
        
        call_id = vp.get('CallId')
        is_scheduled = vp.get('isScheduled', False)
        vp_status = vp.get('status', '')
        
        # Handle scheduled VPs differently
        schedule_cancelled = False
        if is_scheduled and vp_status == 'SCHEDULED':
            logger.info(f"VP {vp_id} is scheduled, attempting to cancel EventBridge schedule")
            schedule_cancelled = self.cancel_eventbridge_schedule(vp_id)
            
            # For scheduled VPs that are cancelled, set status to CANCELLED instead of ENDED
            final_status = 'CANCELLED' if 'cancelled' in end_reason.lower() else 'ENDED'
        else:
            final_status = 'ENDED'
        
        # Send END event to Kinesis (only for active VPs with CallId)
        kinesis_end_success = False
        if call_id:
            try:
                kinesis_end_success = self.send_end_meeting_event(call_id, vp)
                logger.info(f"Sent END event to Kinesis for CallId {call_id}")
            except Exception as e:
                logger.error(f"Failed to send END event to Kinesis: {e}")
        
        # Stop ECS container using registry (only for active VPs)
        ecs_termination_success = False
        if not is_scheduled or vp_status != 'SCHEDULED':
            task_details = self.get_task_details_from_registry(vp_id)
            
            if task_details:
                task_arn = task_details.get('taskArn')
                cluster_arn = task_details.get('clusterArn')
                logger.info(f"Found task details in registry for VP {vp_id}")
                
                # Direct termination using stored ARNs
                ecs_termination_success = self.ecs_manager.stop_vp_task_by_arn(task_arn, cluster_arn, vp_id, end_reason)
                
                # Clean up registry entry
                if ecs_termination_success:
                    self.cleanup_registry_entry(vp_id)
            else:
                logger.warning(f"No task details found in registry for VP {vp_id}")
        
        # Update VP status
        try:
            update_expression = "SET #status = :status, updatedAt = :updated_at, endedAt = :ended_at, endReason = :end_reason"
            expression_values = {
                ':status': final_status,
                ':updated_at': current_time,
                ':ended_at': current_time,
                ':end_reason': end_reason
            }
            
            # Add additional fields based on VP type
            if is_scheduled and vp_status == 'SCHEDULED':
                update_expression += ", scheduleCancelled = :schedule_cancelled"
                expression_values[':schedule_cancelled'] = schedule_cancelled
            else:
                update_expression += ", ecsTerminated = :ecs_terminated, kinesisEndSent = :kinesis_end_sent"
                expression_values[':ecs_terminated'] = ecs_termination_success
                expression_values[':kinesis_end_sent'] = kinesis_end_success
            
            response = self.table.update_item(
                Key={'id': vp_id},
                UpdateExpression=update_expression,
                ExpressionAttributeNames={'#status': 'status'},
                ExpressionAttributeValues=expression_values,
                ReturnValues='ALL_NEW'
            )
            
            logger.info(f"VP {vp_id} terminated successfully with status {final_status}")
            return response['Attributes']
            
        except Exception as e:
            logger.error(f"Error updating VP {vp_id} status: {str(e)}")
            raise
    
    def send_end_meeting_event(self, call_id: str, vp: Dict[str, Any]) -> bool:
        """Send END event to Kinesis to close the meeting transcript"""
        try:
            kinesis_stream_name = os.environ.get('KINESIS_STREAM_NAME')
            if not kinesis_stream_name:
                return False
            
            kinesis = boto3.client('kinesis')
            
            end_call_event = {
                'EventType': 'END',
                'CallId': call_id,
                'CustomerPhoneNumber': 'Virtual Participant',
                'SystemPhoneNumber': 'LMA System',
                'CreatedAt': self.get_current_timestamp(),
                'AgentId': vp.get('owner', 'Unknown'),
                'AccessToken': '',
                'IdToken': '',
                'RefreshToken': '',
            }
            
            kinesis.put_record(
                StreamName=kinesis_stream_name,
                PartitionKey=call_id,
                Data=json.dumps(end_call_event).encode('utf-8')
            )
            
            return True
            
        except Exception as e:
            logger.error(f"Error sending END event to Kinesis: {e}")
            return False
    
    def get_task_details_from_registry(self, vp_id: str) -> Optional[Dict[str, Any]]:
        """Get task details from VPTaskRegistry table"""
        try:
            registry_table_name = os.environ.get('VP_TASK_REGISTRY_TABLE_NAME')
            if not registry_table_name:
                logger.warning("VP_TASK_REGISTRY_TABLE_NAME environment variable not set")
                return None
            
            registry_table_resource = dynamodb.Table(registry_table_name)
            response = registry_table_resource.get_item(Key={'vpId': vp_id})
            
            return response.get('Item')
            
        except Exception as e:
            logger.error(f"Error getting task details from registry: {e}")
            return None
    
    def cleanup_registry_entry(self, vp_id: str) -> bool:
        """Clean up registry entry after task termination"""
        try:
            registry_table_name = os.environ.get('VP_TASK_REGISTRY_TABLE_NAME')
            if not registry_table_name:
                logger.warning("VP_TASK_REGISTRY_TABLE_NAME environment variable not set")
                return False
            
            registry_table_resource = dynamodb.Table(registry_table_name)
            registry_table_resource.delete_item(Key={'vpId': vp_id})
            logger.info(f"Cleaned up registry entry for VP {vp_id}")
            return True
                
        except Exception as e:
            logger.error(f"Error cleaning up registry entry: {e}")
            return False
    
    def cancel_eventbridge_schedule(self, vp_id: str) -> bool:
        """Cancel EventBridge schedule for a scheduled VP"""
        try:
            # Debug all environment variables
            logger.info(f"All environment variables: {dict(os.environ)}")
            
            schedule_group_name = os.environ.get('VP_SCHEDULE_GROUP_NAME')
            logger.info(f"VP_SCHEDULE_GROUP_NAME from environment: '{schedule_group_name}'")
            
            if not schedule_group_name or schedule_group_name.strip() == "":
                logger.warning("VP_SCHEDULE_GROUP_NAME environment variable not set or empty")
                # Try to use a fallback pattern based on the known format
                logger.info("Attempting to use fallback schedule group name pattern")
                # This is a temporary fallback - ideally the environment variable should be set
                schedule_group_name = "LMA-VIRTUALPARTICIPANTSTACK-1ICEYE8S4JXFV-vp-schedules"
                logger.info(f"Using fallback schedule group name: {schedule_group_name}")
            
            # The schedule name is the VP ID (as set by the VirtualParticipantSchedulerFunction)
            schedule_name = vp_id
            
            logger.info(f"Attempting to delete schedule {schedule_name} from group {schedule_group_name}")
            
            scheduler.delete_schedule(
                GroupName=schedule_group_name,
                Name=schedule_name
            )
            
            logger.info(f"Successfully cancelled EventBridge schedule for VP {vp_id}")
            return True
            
        except scheduler.exceptions.ResourceNotFoundException:
            logger.warning(f"Schedule for VP {vp_id} not found (may have already been executed or deleted)")
            return True  # Consider this a success since the schedule is gone
            
        except Exception as e:
            logger.error(f"Error cancelling EventBridge schedule for VP {vp_id}: {str(e)}")
            return False

def lambda_handler(event, context):
    """Lambda handler for Virtual Participant management operations"""
    
    logger.info("Processing VP management request")
    
    table_name = os.environ.get('TABLE_NAME', 'VirtualParticipants')
    vp_manager = VirtualParticipantManager(table_name)
    
    try:
        operation = event.get('operation')
        arguments = event.get('arguments', {})
        
        if operation == 'endVirtualParticipant':
            input_data = arguments.get('input', {})
            
            result = vp_manager.end_virtual_participant(
                vp_id=input_data['id'],
                end_reason=input_data['endReason'],
                ended_by=input_data.get('endedBy')
            )
            
            logger.info("VP termination completed successfully")
            
        else:
            raise ValueError(f"Unknown operation: {operation}")
        
        return {
            'statusCode': 200,
            'body': result
        }
        
    except Exception as e:
        logger.error(f"Error processing request: {str(e)}")
        
        return {
            'statusCode': 500,
            'body': {
                'error': str(e)
            }
        }
