# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""
ALB Cleanup Manager for Virtual Participants
Handles cleanup of ALB target groups and listener rules
"""

import boto3
import logging
from typing import Optional

logger = logging.getLogger(__name__)

class ALBCleanupManager:
    """
    Manages cleanup of ALB resources for Virtual Participants
    """
    
    def __init__(self):
        self.elbv2_client = boto3.client('elbv2')
        logger.info("ALBCleanupManager initialized")
    
    def cleanup_vp_alb_resources(self, vp_id: str, listener_arn: str) -> bool:
        """
        Clean up ALB resources (target group and listener rule) for a VP
        
        Args:
            vp_id: Virtual Participant ID
            listener_arn: ARN of the ALB listener
            
        Returns:
            True if cleanup was successful, False otherwise
        """
        try:
            logger.info(f"Starting ALB cleanup for VP {vp_id}")
            
            # Step 1: Find target group by name pattern
            tg_name = f"vnc-{vp_id[:27]}"
            target_group_arn = self._find_target_group(tg_name)
            
            # Step 2: Find listener rule by tag
            rule_arn = self._find_listener_rule(listener_arn, vp_id)
            
            # Step 3: Delete listener rule first (must be done before target group)
            rule_deleted = False
            if rule_arn:
                rule_deleted = self._delete_listener_rule(rule_arn)
            else:
                logger.warning(f"No listener rule found for VP {vp_id}")
            
            # Step 4: Delete target group
            tg_deleted = False
            if target_group_arn:
                tg_deleted = self._delete_target_group(target_group_arn)
            else:
                logger.warning(f"No target group found for VP {vp_id}")
            
            success = rule_deleted and tg_deleted
            if success:
                logger.info(f"Successfully cleaned up ALB resources for VP {vp_id}")
            else:
                logger.warning(f"Partial cleanup for VP {vp_id} - Rule: {rule_deleted}, TG: {tg_deleted}")
            
            return success
            
        except Exception as e:
            logger.error(f"Error cleaning up ALB resources for VP {vp_id}: {str(e)}")
            return False
    
    def _find_target_group(self, tg_name: str) -> Optional[str]:
        """
        Find target group ARN by name
        
        Args:
            tg_name: Target group name
            
        Returns:
            Target group ARN if found, None otherwise
        """
        try:
            logger.info(f"Looking for target group: {tg_name}")
            
            response = self.elbv2_client.describe_target_groups(
                Names=[tg_name]
            )
            
            if response['TargetGroups']:
                tg_arn = response['TargetGroups'][0]['TargetGroupArn']
                logger.info(f"Found target group: {tg_arn}")
                return tg_arn
            
            logger.warning(f"Target group not found: {tg_name}")
            return None
            
        except self.elbv2_client.exceptions.TargetGroupNotFoundException:
            logger.info(f"Target group not found (already deleted): {tg_name}")
            return None
        except Exception as e:
            logger.error(f"Error finding target group {tg_name}: {str(e)}")
            return None
    
    def _find_listener_rule(self, listener_arn: str, vp_id: str) -> Optional[str]:
        """
        Find listener rule ARN by VirtualParticipantId tag
        
        Args:
            listener_arn: ARN of the ALB listener
            vp_id: Virtual Participant ID
            
        Returns:
            Listener rule ARN if found, None otherwise
        """
        try:
            logger.info(f"Looking for listener rule for VP {vp_id}")
            
            # Get all rules for the listener
            response = self.elbv2_client.describe_rules(
                ListenerArn=listener_arn
            )
            
            # Find rule with matching VirtualParticipantId tag
            for rule in response['Rules']:
                rule_arn = rule['RuleArn']
                
                # Get tags for this rule
                try:
                    tags_response = self.elbv2_client.describe_tags(
                        ResourceArns=[rule_arn]
                    )
                    
                    if tags_response['TagDescriptions']:
                        tags = tags_response['TagDescriptions'][0]['Tags']
                        
                        # Check for VirtualParticipantId tag
                        for tag in tags:
                            if tag['Key'] == 'VirtualParticipantId' and tag['Value'] == vp_id:
                                logger.info(f"Found listener rule: {rule_arn}")
                                return rule_arn
                
                except Exception as e:
                    logger.debug(f"Error getting tags for rule {rule_arn}: {str(e)}")
                    continue
            
            logger.warning(f"No listener rule found for VP {vp_id}")
            return None
            
        except Exception as e:
            logger.error(f"Error finding listener rule for VP {vp_id}: {str(e)}")
            return None
    
    def _delete_listener_rule(self, rule_arn: str) -> bool:
        """
        Delete a listener rule
        
        Args:
            rule_arn: ARN of the listener rule to delete
            
        Returns:
            True if successful, False otherwise
        """
        try:
            logger.info(f"Deleting listener rule: {rule_arn}")
            
            self.elbv2_client.delete_rule(
                RuleArn=rule_arn
            )
            
            logger.info(f"Successfully deleted listener rule: {rule_arn}")
            return True
            
        except self.elbv2_client.exceptions.RuleNotFoundException:
            logger.info(f"Listener rule already deleted: {rule_arn}")
            return True  # Consider this success since it's gone
        except Exception as e:
            logger.error(f"Error deleting listener rule {rule_arn}: {str(e)}")
            return False
    
    def _delete_target_group(self, tg_arn: str) -> bool:
        """
        Delete a target group (after deregistering all targets)
        
        Args:
            tg_arn: ARN of the target group to delete
            
        Returns:
            True if successful, False otherwise
        """
        try:
            logger.info(f"Deleting target group: {tg_arn}")
            
            # First, deregister all targets
            try:
                # Get current targets
                health_response = self.elbv2_client.describe_target_health(
                    TargetGroupArn=tg_arn
                )
                
                if health_response['TargetHealthDescriptions']:
                    targets = [
                        {'Id': target['Target']['Id'], 'Port': target['Target']['Port']}
                        for target in health_response['TargetHealthDescriptions']
                    ]
                    
                    if targets:
                        logger.info(f"Deregistering {len(targets)} targets from target group")
                        self.elbv2_client.deregister_targets(
                            TargetGroupArn=tg_arn,
                            Targets=targets
                        )
                        logger.info("Targets deregistered successfully")
            
            except Exception as e:
                logger.warning(f"Error deregistering targets (may already be deregistered): {str(e)}")
            
            # Now delete the target group
            self.elbv2_client.delete_target_group(
                TargetGroupArn=tg_arn
            )
            
            logger.info(f"Successfully deleted target group: {tg_arn}")
            return True
            
        except self.elbv2_client.exceptions.TargetGroupNotFoundException:
            logger.info(f"Target group already deleted: {tg_arn}")
            return True  # Consider this success since it's gone
        except Exception as e:
            logger.error(f"Error deleting target group {tg_arn}: {str(e)}")
            return False
