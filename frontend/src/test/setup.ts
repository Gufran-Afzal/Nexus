import '@testing-library/jest-dom/vitest';
// jsdom doesn't implement scrolling.
Element.prototype.scrollIntoView = function scrollIntoView() {};
