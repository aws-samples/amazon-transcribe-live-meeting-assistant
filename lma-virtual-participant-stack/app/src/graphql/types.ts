/* tslint:disable */
/* eslint-disable */
//  This file was automatically generated and should not be edited.

export type CreateInviteInput = {
  name: string,
  meetingPlatform: string,
  meetingId: string,
  meetingPassword?: string | null,
  meetingTime: number,
  status?: string | null,
  users?: Array< string | null > | null,
  id?: string | null,
};

export type ModelInviteConditionInput = {
  name?: ModelStringInput | null,
  meetingPlatform?: ModelStringInput | null,
  meetingId?: ModelStringInput | null,
  meetingPassword?: ModelStringInput | null,
  meetingTime?: ModelIntInput | null,
  status?: ModelStringInput | null,
  users?: ModelStringInput | null,
  and?: Array< ModelInviteConditionInput | null > | null,
  or?: Array< ModelInviteConditionInput | null > | null,
  not?: ModelInviteConditionInput | null,
  createdAt?: ModelStringInput | null,
  updatedAt?: ModelStringInput | null,
};

export type ModelStringInput = {
  ne?: string | null,
  eq?: string | null,
  le?: string | null,
  lt?: string | null,
  ge?: string | null,
  gt?: string | null,
  contains?: string | null,
  notContains?: string | null,
  between?: Array< string | null > | null,
  beginsWith?: string | null,
  attributeExists?: boolean | null,
  attributeType?: ModelAttributeTypes | null,
  size?: ModelSizeInput | null,
};

export enum ModelAttributeTypes {
  binary = "binary",
  binarySet = "binarySet",
  bool = "bool",
  list = "list",
  map = "map",
  number = "number",
  numberSet = "numberSet",
  string = "string",
  stringSet = "stringSet",
  _null = "_null",
}


export type ModelSizeInput = {
  ne?: number | null,
  eq?: number | null,
  le?: number | null,
  lt?: number | null,
  ge?: number | null,
  gt?: number | null,
  between?: Array< number | null > | null,
};

export type ModelIntInput = {
  ne?: number | null,
  eq?: number | null,
  le?: number | null,
  lt?: number | null,
  ge?: number | null,
  gt?: number | null,
  between?: Array< number | null > | null,
  attributeExists?: boolean | null,
  attributeType?: ModelAttributeTypes | null,
};

export type Invite = {
  __typename: "Invite",
  name: string,
  meetingPlatform: string,
  meetingId: string,
  meetingPassword?: string | null,
  meetingTime: number,
  status?: string | null,
  users?: Array< string | null > | null,
  id: string,
  createdAt: string,
  updatedAt: string,
};

export type UpdateInviteInput = {
  name?: string | null,
  meetingPlatform?: string | null,
  meetingId?: string | null,
  meetingPassword?: string | null,
  meetingTime?: number | null,
  status?: string | null,
  users?: Array< string | null > | null,
  id: string,
};

export type DeleteInviteInput = {
  id: string,
};

export type ModelInviteFilterInput = {
  name?: ModelStringInput | null,
  meetingPlatform?: ModelStringInput | null,
  meetingId?: ModelStringInput | null,
  meetingPassword?: ModelStringInput | null,
  meetingTime?: ModelIntInput | null,
  status?: ModelStringInput | null,
  users?: ModelStringInput | null,
  id?: ModelIDInput | null,
  createdAt?: ModelStringInput | null,
  updatedAt?: ModelStringInput | null,
  and?: Array< ModelInviteFilterInput | null > | null,
  or?: Array< ModelInviteFilterInput | null > | null,
  not?: ModelInviteFilterInput | null,
};

export type ModelIDInput = {
  ne?: string | null,
  eq?: string | null,
  le?: string | null,
  lt?: string | null,
  ge?: string | null,
  gt?: string | null,
  contains?: string | null,
  notContains?: string | null,
  between?: Array< string | null > | null,
  beginsWith?: string | null,
  attributeExists?: boolean | null,
  attributeType?: ModelAttributeTypes | null,
  size?: ModelSizeInput | null,
};

export type ModelInviteConnection = {
  __typename: "ModelInviteConnection",
  items:  Array<Invite | null >,
  nextToken?: string | null,
};

export type ModelSubscriptionInviteFilterInput = {
  name?: ModelSubscriptionStringInput | null,
  meetingPlatform?: ModelSubscriptionStringInput | null,
  meetingId?: ModelSubscriptionStringInput | null,
  meetingPassword?: ModelSubscriptionStringInput | null,
  meetingTime?: ModelSubscriptionIntInput | null,
  status?: ModelSubscriptionStringInput | null,
  id?: ModelSubscriptionIDInput | null,
  createdAt?: ModelSubscriptionStringInput | null,
  updatedAt?: ModelSubscriptionStringInput | null,
  and?: Array< ModelSubscriptionInviteFilterInput | null > | null,
  or?: Array< ModelSubscriptionInviteFilterInput | null > | null,
  users?: ModelStringInput | null,
};

export type ModelSubscriptionStringInput = {
  ne?: string | null,
  eq?: string | null,
  le?: string | null,
  lt?: string | null,
  ge?: string | null,
  gt?: string | null,
  contains?: string | null,
  notContains?: string | null,
  between?: Array< string | null > | null,
  beginsWith?: string | null,
  in?: Array< string | null > | null,
  notIn?: Array< string | null > | null,
};

export type ModelSubscriptionIntInput = {
  ne?: number | null,
  eq?: number | null,
  le?: number | null,
  lt?: number | null,
  ge?: number | null,
  gt?: number | null,
  between?: Array< number | null > | null,
  in?: Array< number | null > | null,
  notIn?: Array< number | null > | null,
};

export type ModelSubscriptionIDInput = {
  ne?: string | null,
  eq?: string | null,
  le?: string | null,
  lt?: string | null,
  ge?: string | null,
  gt?: string | null,
  contains?: string | null,
  notContains?: string | null,
  between?: Array< string | null > | null,
  beginsWith?: string | null,
  in?: Array< string | null > | null,
  notIn?: Array< string | null > | null,
};

export type CreateInviteMutationVariables = {
  input: CreateInviteInput,
  condition?: ModelInviteConditionInput | null,
};

export type CreateInviteMutation = {
  createInvite?:  {
    __typename: "Invite",
    name: string,
    meetingPlatform: string,
    meetingId: string,
    meetingPassword?: string | null,
    meetingTime: number,
    status?: string | null,
    users?: Array< string | null > | null,
    id: string,
    createdAt: string,
    updatedAt: string,
  } | null,
};

export type UpdateInviteMutationVariables = {
  input: UpdateInviteInput,
  condition?: ModelInviteConditionInput | null,
};

export type UpdateInviteMutation = {
  updateInvite?:  {
    __typename: "Invite",
    name: string,
    meetingPlatform: string,
    meetingId: string,
    meetingPassword?: string | null,
    meetingTime: number,
    status?: string | null,
    users?: Array< string | null > | null,
    id: string,
    createdAt: string,
    updatedAt: string,
  } | null,
};

export type DeleteInviteMutationVariables = {
  input: DeleteInviteInput,
  condition?: ModelInviteConditionInput | null,
};

export type DeleteInviteMutation = {
  deleteInvite?:  {
    __typename: "Invite",
    name: string,
    meetingPlatform: string,
    meetingId: string,
    meetingPassword?: string | null,
    meetingTime: number,
    status?: string | null,
    users?: Array< string | null > | null,
    id: string,
    createdAt: string,
    updatedAt: string,
  } | null,
};

export type GetInviteQueryVariables = {
  id: string,
};

export type GetInviteQuery = {
  getInvite?:  {
    __typename: "Invite",
    name: string,
    meetingPlatform: string,
    meetingId: string,
    meetingPassword?: string | null,
    meetingTime: number,
    status?: string | null,
    users?: Array< string | null > | null,
    id: string,
    createdAt: string,
    updatedAt: string,
  } | null,
};

export type ListInvitesQueryVariables = {
  filter?: ModelInviteFilterInput | null,
  limit?: number | null,
  nextToken?: string | null,
};

export type ListInvitesQuery = {
  listInvites?:  {
    __typename: "ModelInviteConnection",
    items:  Array< {
      __typename: "Invite",
      name: string,
      meetingPlatform: string,
      meetingId: string,
      meetingPassword?: string | null,
      meetingTime: number,
      status?: string | null,
      users?: Array< string | null > | null,
      id: string,
      createdAt: string,
      updatedAt: string,
    } | null >,
    nextToken?: string | null,
  } | null,
};

export type OnCreateInviteSubscriptionVariables = {
  filter?: ModelSubscriptionInviteFilterInput | null,
};

export type OnCreateInviteSubscription = {
  onCreateInvite?:  {
    __typename: "Invite",
    name: string,
    meetingPlatform: string,
    meetingId: string,
    meetingPassword?: string | null,
    meetingTime: number,
    status?: string | null,
    users?: Array< string | null > | null,
    id: string,
    createdAt: string,
    updatedAt: string,
  } | null,
};

export type OnUpdateInviteSubscriptionVariables = {
  filter?: ModelSubscriptionInviteFilterInput | null,
};

export type OnUpdateInviteSubscription = {
  onUpdateInvite?:  {
    __typename: "Invite",
    name: string,
    meetingPlatform: string,
    meetingId: string,
    meetingPassword?: string | null,
    meetingTime: number,
    status?: string | null,
    users?: Array< string | null > | null,
    id: string,
    createdAt: string,
    updatedAt: string,
  } | null,
};

export type OnDeleteInviteSubscriptionVariables = {
  filter?: ModelSubscriptionInviteFilterInput | null,
};

export type OnDeleteInviteSubscription = {
  onDeleteInvite?:  {
    __typename: "Invite",
    name: string,
    meetingPlatform: string,
    meetingId: string,
    meetingPassword?: string | null,
    meetingTime: number,
    status?: string | null,
    users?: Array< string | null > | null,
    id: string,
    createdAt: string,
    updatedAt: string,
  } | null,
};
