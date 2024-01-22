/* eslint-disable react/no-array-index-key */
import React from 'react';
import { PropTypes } from 'prop-types';

import useSettingsContext from '../../contexts/settings';

import './CategoryPill.css';

// eslint-disable-next-line import/prefer-default-export, arrow-body-style
export const CategoryPills = (props) => {
  const { categories } = props;
  const { settings } = useSettingsContext();

  const regex = settings.CategoryAlertRegex ?? '.*';

  if (categories) {
    let alerts = [];
    const normal = [];
    // eslint-disable-next-line array-callback-return
    categories.map((category, index) => {
      if (category.match(regex)) {
        alerts.push(
          <span key={index} className="category-pill-alert">
            {category}
          </span>,
        );
      } else {
        normal.push(
          <span key={index} className="category-pill">
            {category}
          </span>,
        );
      }
    });

    alerts = alerts.concat(normal);

    return <div>{alerts}</div>;
  }

  return null;

  /* const pills = [];
  if (categories) {
    categories.forEach((category) => {
      pills.push(<span className="category-pill-alert">{category}</span>);
    });
  }

  if (pills.length === 0) return null;

  return { pills }; */
};

CategoryPills.propTypes = {
  categories: PropTypes.arrayOf(PropTypes.string),
};

CategoryPills.defaultProps = {
  categories: [],
};
