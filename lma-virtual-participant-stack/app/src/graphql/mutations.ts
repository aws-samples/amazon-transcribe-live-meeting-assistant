/* tslint:disable */
/* eslint-disable */
// this is an auto generated file. This will be overwritten

import * as APITypes from "./types";
type GeneratedMutation<InputType, OutputType> = string & {
  __generatedMutationInput: InputType;
  __generatedMutationOutput: OutputType;
};

export const createInvite = /* GraphQL */ `mutation CreateInvite(
  $input: CreateInviteInput!
  $condition: ModelInviteConditionInput
) {
  createInvite(input: $input, condition: $condition) {
    name
    meetingPlatform
    meetingId
    meetingPassword
    meetingTime
    status
    users
    id
    createdAt
    updatedAt
    __typename
  }
}
` as GeneratedMutation<
  APITypes.CreateInviteMutationVariables,
  APITypes.CreateInviteMutation
>;
export const updateInvite = /* GraphQL */ `mutation UpdateInvite(
  $input: UpdateInviteInput!
  $condition: ModelInviteConditionInput
) {
  updateInvite(input: $input, condition: $condition) {
    name
    meetingPlatform
    meetingId
    meetingPassword
    meetingTime
    status
    users
    id
    createdAt
    updatedAt
    __typename
  }
}
` as GeneratedMutation<
  APITypes.UpdateInviteMutationVariables,
  APITypes.UpdateInviteMutation
>;
export const deleteInvite = /* GraphQL */ `mutation DeleteInvite(
  $input: DeleteInviteInput!
  $condition: ModelInviteConditionInput
) {
  deleteInvite(input: $input, condition: $condition) {
    name
    meetingPlatform
    meetingId
    meetingPassword
    meetingTime
    status
    users
    id
    createdAt
    updatedAt
    __typename
  }
}
` as GeneratedMutation<
  APITypes.DeleteInviteMutationVariables,
  APITypes.DeleteInviteMutation
>;
