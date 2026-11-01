/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
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
    // Every mutation published to a subscription using this resolver sends
    // SharedWith as "[a, b]" (see the "SharedWith published" block in
    // dDbPutCondition.response.vtl, and shareMeetings.js / endVirtualParticipant.js),
    // so the caller is an entry exactly when the value is "[caller]", or the
    // caller is followed by ", " at the start, or is between ", " and ", " or
    // ", " and "]". Entries are compared whole, as getCall compares them.
    // "[", "]" and "," only ever separate entries -- the published form holds
    // no entry containing one -- so a name containing one is matched as the
    // Owner only.
    const { username } = ctx.identity;
    const rules = [{ Owner: { eq: username } }];
    if (typeof username === 'string' && !['[', ']', ','].some((separator) => username.includes(separator))) {
      rules.push(
        { SharedWith: { eq: `[${username}]` } },
        { SharedWith: { beginsWith: `[${username}, ` } },
        { SharedWith: { contains: `, ${username}, ` } },
        { SharedWith: { contains: `, ${username}]` } },
      );
    }
    const filter = { or: rules };
    console.debug(`subscription filter: ${JSON.stringify(filter)}`);
    extensions.setSubscriptionFilter(util.transform.toSubscriptionFilter(filter));
  }
  return null;
}