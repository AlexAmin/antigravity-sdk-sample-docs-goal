/**
 * @OnlyCurrentDoc
 */

function onOpen() {
  DocumentApp.getUi()
      .createMenu('Internal Tools')
      .addItem('Open Company Knowledge Assistant', 'showSidebar')
      .addToUi();
}

function showSidebar() {
  var html = HtmlService.createHtmlOutputFromFile('Sidebar')
      .setTitle('Company Knowledge Assistant')
      .setWidth(450)
      .setHeight(650);
  DocumentApp.getUi().showModelessDialog(html, 'Company Knowledge Assistant');
}

function getActiveDocId() {
  return DocumentApp.getActiveDocument().getId();
}
