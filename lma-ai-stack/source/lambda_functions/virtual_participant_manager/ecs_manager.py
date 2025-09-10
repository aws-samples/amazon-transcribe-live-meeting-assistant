"""
ECS Task Manager for Virtual Participants
Handles actual termination of running ECS containers
"""

import boto3
import logging
from typing import Dict, List, Optional
import os

logger = logging.getLogger(__name__)

class ECSTaskManager:
    """
    Manages ECS tasks for Virtual Participants
    """
    
    def __init__(self):
        self.ecs_client = boto3.client('ecs')
        self.cluster_name = os.environ.get('ECS_CLUSTER_NAME', 'VirtualParticipantCluster')
        
    def find_vp_task(self, vp_id: str) -> Optional[str]:
        """
        Find the running ECS task for a Virtual Participant
        
        Args:
            vp_id: Virtual Participant ID
            
        Returns:
            Task ARN if found, None otherwise
        """
        try:
            # List all running tasks in the cluster
            response = self.ecs_client.list_tasks(
                cluster=self.cluster_name,
                desiredStatus='RUNNING'
            )
            
            if not response.get('taskArns'):
                logger.info(f"No running tasks found in cluster {self.cluster_name}")
                return None
            
            # Describe tasks to get details
            task_details = self.ecs_client.describe_tasks(
                cluster=self.cluster_name,
                tasks=response['taskArns']
            )
            
            # Find task with matching VP ID in environment variables
            for task in task_details.get('tasks', []):
                task_definition = task.get('taskDefinitionArn', '')
                
                # Get task definition details to check environment variables
                task_def_response = self.ecs_client.describe_task_definition(
                    taskDefinition=task_definition
                )
                
                containers = task_def_response.get('taskDefinition', {}).get('containerDefinitions', [])
                
                for container in containers:
                    env_vars = container.get('environment', [])
                    
                    # Look for VIRTUAL_PARTICIPANT_ID environment variable
                    for env_var in env_vars:
                        if (env_var.get('name') == 'VIRTUAL_PARTICIPANT_ID' and 
                            env_var.get('value') == vp_id):
                            logger.info(f"Found ECS task for VP {vp_id}: {task['taskArn']}")
                            return task['taskArn']
            
            logger.info(f"No ECS task found for VP {vp_id}")
            return None
            
        except Exception as e:
            logger.error(f"Error finding ECS task for VP {vp_id}: {str(e)}")
            return None
    
    def stop_vp_task(self, vp_id: str, reason: str = "User requested termination") -> bool:
        """
        Stop the ECS task for a Virtual Participant
        
        Args:
            vp_id: Virtual Participant ID
            reason: Reason for stopping the task
            
        Returns:
            True if task was stopped successfully, False otherwise
        """
        try:
            # Find the running task
            task_arn = self.find_vp_task(vp_id)
            
            if not task_arn:
                logger.warning(f"No running ECS task found for VP {vp_id}")
                return False
            
            # Stop the task
            response = self.ecs_client.stop_task(
                cluster=self.cluster_name,
                task=task_arn,
                reason=reason
            )
            
            stopped_task = response.get('task', {})
            logger.info(f"Successfully stopped ECS task for VP {vp_id}: {task_arn}")
            logger.info(f"Task status: {stopped_task.get('lastStatus')}")
            
            return True
            
        except Exception as e:
            logger.error(f"Error stopping ECS task for VP {vp_id}: {str(e)}")
            return False
    
    def get_task_status(self, vp_id: str) -> Dict:
        """
        Get the current status of the ECS task for a Virtual Participant
        
        Args:
            vp_id: Virtual Participant ID
            
        Returns:
            Dictionary with task status information
        """
        try:
            task_arn = self.find_vp_task(vp_id)
            
            if not task_arn:
                return {
                    'found': False,
                    'status': 'NOT_FOUND',
                    'message': 'No running ECS task found'
                }
            
            # Get task details
            response = self.ecs_client.describe_tasks(
                cluster=self.cluster_name,
                tasks=[task_arn]
            )
            
            if response.get('tasks'):
                task = response['tasks'][0]
                return {
                    'found': True,
                    'status': task.get('lastStatus'),
                    'desired_status': task.get('desiredStatus'),
                    'health_status': task.get('healthStatus'),
                    'created_at': task.get('createdAt'),
                    'started_at': task.get('startedAt'),
                    'stopped_at': task.get('stoppedAt'),
                    'stop_reason': task.get('stoppedReason'),
                    'task_arn': task_arn
                }
            
            return {
                'found': False,
                'status': 'UNKNOWN',
                'message': 'Task details not available'
            }
            
        except Exception as e:
            logger.error(f"Error getting task status for VP {vp_id}: {str(e)}")
            return {
                'found': False,
                'status': 'ERROR',
                'message': str(e)
            }
    
    def list_all_vp_tasks(self) -> List[Dict]:
        """
        List all running Virtual Participant tasks
        
        Returns:
            List of task information dictionaries
        """
        try:
            # List all running tasks
            response = self.ecs_client.list_tasks(
                cluster=self.cluster_name,
                desiredStatus='RUNNING'
            )
            
            if not response.get('taskArns'):
                return []
            
            # Get task details
            task_details = self.ecs_client.describe_tasks(
                cluster=self.cluster_name,
                tasks=response['taskArns']
            )
            
            vp_tasks = []
            
            for task in task_details.get('tasks', []):
                # Extract VP ID from task if possible
                task_info = {
                    'task_arn': task['taskArn'],
                    'status': task.get('lastStatus'),
                    'desired_status': task.get('desiredStatus'),
                    'created_at': task.get('createdAt'),
                    'started_at': task.get('startedAt'),
                    'vp_id': None  # Will be populated if found
                }
                
                # Try to extract VP ID from task definition
                task_definition = task.get('taskDefinitionArn', '')
                if 'VirtualParticipant' in task_definition:
                    # This is likely a VP task
                    vp_tasks.append(task_info)
            
            return vp_tasks
            
        except Exception as e:
            logger.error(f"Error listing VP tasks: {str(e)}")
            return []
