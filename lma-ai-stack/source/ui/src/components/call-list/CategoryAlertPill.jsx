import React from 'react';
import { PropTypes } from 'prop-types';

import './CategoryPill.css';
import { Popover } from '@awsui/components-react';
import useSettingsContext from '../../contexts/settings';

// eslint-disable-next-line import/prefer-default-export, arrow-body-style
export const CategoryAlertPill = (props) => {
  const { settings } = useSettingsContext();
  const regex = settings.CategoryAlertRegex ?? '.*';

  const { alertCount, categories } = props;

  const matchList = [];
  if (categories) {
    categories.forEach((category) => {
      if (category.match(regex)) {
        matchList.push(category);
      }
    });
  }

  if (categories?.length === 0 || alertCount === 0) return null;
  const popup = matchList.map((category) => (
    <>
      <span className="category-pill-alert">{category}</span>
      <br />
    </>
  ));
  return (
    <Popover dismissButton={false} position="right" size="small" triggerType="custom" content={popup}>
      <span className="category-pill-alert-icon">{alertCount}</span>
    </Popover>
  );
};

CategoryAlertPill.propTypes = {
  categories: PropTypes.arrayOf(PropTypes.string),
  alertCount: PropTypes.number,
};

CategoryAlertPill.defaultProps = {
  categories: [],
  alertCount: 0,
};
