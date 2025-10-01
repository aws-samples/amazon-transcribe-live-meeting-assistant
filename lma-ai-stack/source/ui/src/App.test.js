/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import App from './App';

test('renders app div element', () => {
  render(<App />);
  const divElement = screen.getByText(
    // eslint-disable-next-line prettier/prettier
    (content, element) => element.tagName.toLowerCase() === 'div' && element.className.includes('App'),
  );
  expect(divElement).toBeInTheDocument();
});
