/* CSV cells for exports built in the browser (Bulk order, Invoices). Lines come from customers' files and records;
   a cell that starts with = + - @ (or a tab or carriage return) would run as a formula in Excel or Sheets, so it gets a
   leading apostrophe unless it's a plain number. Loaded before app.js. */
"use strict";
function csvCell(v) {
  let s = String(v ?? "");
  if (/^[=+\-@\t\r]/.test(s) && !/^[+-]?\d+(\.\d+)?$/.test(s)) s = "'" + s;
  return `"${s.replace(/"/g, '""')}"`;
}
