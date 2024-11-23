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

  if (!selectionSetList.includes('SharedWith')) {
    console.error('You must included the "SharedWith field in the selection set');
    util.unauthorized();
  }

  console.debug(`Setting subscription filter with SharedWith`);
  const filter = {
      SharedWith: { contains: ctx.identity.username }
  };
  extensions.setSubscriptionFilter(util.transform.toSubscriptionFilter(filter));

  return null;
}