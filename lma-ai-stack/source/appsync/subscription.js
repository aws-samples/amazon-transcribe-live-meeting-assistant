/*
 * 
 * Copyright Amazon.com, Inc. or its affiliates. This material is AWS Content under the AWS Enterprise Agreement 
 * or AWS Customer Agreement (as applicable) and is provided under the AWS Intellectual Property License.
 * 
 */
export function request() {
  return { payload: null };
}

/**
 * @param {import('@aws-appsync/utils').Context} ctx the context
 * @returns {*} the request
 */
export function response(ctx) {
  const { fieldName, parentTypeName, selectionSetGraphQL, selectionSetList, variables } = ctx.info;

  console.debug(`/*****/`);
  console.debug(`setting up subscription for user ${ctx.identity.username}`);
  console.debug(`fieldName: ${fieldName}`);
  console.debug(`parentTypeName: ${parentTypeName}`);
  console.debug(`selectionSetGraphQL: ${selectionSetGraphQL}`);
  console.debug(`selectionSetList: ${selectionSetList}`);
  console.debug(`variables: ${variables}`);
  console.debug(`/*****/`);

  if (!selectionSetList.includes('Owner') || !selectionSetList.includes('SharedWith')) {
    console.error('You must included the "Owner" & "SharedWith fields in the selection set');
    util.unauthorized();
  }

  const { groups } = ctx.identity;
  if((groups === undefined) || !ctx.identity.groups.includes("Admin")) {
    console.debug(`Setting subscription filter with Owner or User that have been give access`);
    const filter = {
      or: [
        { Owner: { eq: ctx.identity.username } },
        { SharedWith: { contains: ctx.identity.username } }
      ]
    };
    extensions.setSubscriptionFilter(util.transform.toSubscriptionFilter(filter));
  }
  return null;
}