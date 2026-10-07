/* Shared safe DOM and amount display primitives; no requests or chat state. */
(() => {
  'use strict';
const money = (value) => Number(value).toLocaleString('zh-CN', {minimumFractionDigits: 2, maximumFractionDigits: 2});
function node(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text !== undefined) el.textContent = text;
  return el;
}
function renderMoney(element, value) {
  if(!element)return;
  element.replaceChildren(node('span','money-currency','¥'),node('span','money-value',money(value)));
}
  window.NexusUI = Object.freeze({node, money, renderMoney});
})();
