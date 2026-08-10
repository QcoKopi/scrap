/**
 * Roastery IG Keyword Research - Google Apps Script Web App
 * -----------------------------------------------------------
 * ONE spreadsheet, ONE deployment URL, THREE sheet tabs:
 *   - "Keyword" : input list of search phrases
 *   - "Hasil"   : organic search results (incl. Instagram handles found)
 *   - "Bio"     : Instagram profile bio data, one row per unique handle
 *
 * doGet:
 *   ?action=keywordQueue (default) -> unprocessed rows from "Keyword"
 *   ?action=bioQueue              -> unique real IG handles from "Hasil"
 *                                     that don't have a "Bio" row yet
 *
 * doPost body:
 *   { ...,              "items": [...] }              -> appends to "Hasil" (default)
 *   { "type": "bio",    "items": [...] }               -> appends to "Bio"
 *
 * Auth: optional shared-secret token (Script Property SHARED_TOKEN), see
 * isAuthorized_(). Skipped entirely if the property isn't set.
 */

var IG_PLACEHOLDER_HANDLES = ["@reel", "@p", "@instagram", ""];

function isAuthorized_(params) {
  var expected = PropertiesService.getScriptProperties().getProperty('SHARED_TOKEN');
  if (!expected) return true; // no token configured -> open (legacy behavior)
  return params && params.token === expected;
}

function isRealHandle_(handle) {
  if (!handle) return false;
  return IG_PLACEHOLDER_HANDLES.indexOf(handle) === -1;
}

function doGet(e) {
  try {
    var action = (e && e.parameter && e.parameter.action) || 'keywordQueue';
    if (action === 'bioQueue') {
      return bioQueueResponse_();
    }
    return keywordQueueResponse_();
  } catch (err) {
    return ContentService.createTextOutput(JSON.stringify([]))
                         .setMimeType(ContentService.MimeType.JSON);
  }
}

function keywordQueueResponse_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheetKw = ss.getSheetByName("Keyword");
  var sheetRes = ss.getSheetByName("Hasil");

  if (!sheetKw || !sheetRes) {
    return ContentService.createTextOutput(JSON.stringify([]))
                         .setMimeType(ContentService.MimeType.JSON);
  }

  var dataKw = sheetKw.getDataRange().getValues();
  var dataRes = sheetRes.getDataRange().getValues();

  var processedKeywords = {};
  for (var j = 1; j < dataRes.length; j++) {
    if (dataRes[j] && dataRes[j][1]) {
      processedKeywords[dataRes[j][1]] = true;
    }
  }

  var queueList = [];
  for (var i = 1; i < dataKw.length; i++) {
    var row = dataKw[i];
    if (row && row.length > 2) {
      var keywordVal = row[2]; // Kolom C
      if (keywordVal && !processedKeywords[keywordVal]) {
        queueList.push({
          row: i + 1,
          account: keywordVal
        });
      }
    }
  }

  return ContentService.createTextOutput(JSON.stringify(queueList))
                       .setMimeType(ContentService.MimeType.JSON);
}

function bioQueueResponse_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheetRes = ss.getSheetByName("Hasil");
  var sheetBio = ss.getSheetByName("Bio");

  if (!sheetRes) {
    return ContentService.createTextOutput(JSON.stringify([]))
                         .setMimeType(ContentService.MimeType.JSON);
  }

  var dataRes = sheetRes.getDataRange().getValues();
  // "Instagram Handle" is column K (index 10) per the Hasil header row.
  var seenHandles = {};
  var candidates = [];
  for (var i = 1; i < dataRes.length; i++) {
    var handle = dataRes[i] && dataRes[i][10];
    if (isRealHandle_(handle) && !seenHandles[handle]) {
      seenHandles[handle] = true;
      candidates.push(handle);
    }
  }

  var alreadyDone = {};
  if (sheetBio) {
    var dataBio = sheetBio.getDataRange().getValues();
    for (var k = 1; k < dataBio.length; k++) {
      if (dataBio[k] && dataBio[k][1]) {
        alreadyDone[dataBio[k][1]] = true;
      }
    }
  }

  var queueList = candidates
    .filter(function (h) { return !alreadyDone[h]; })
    .map(function (h) { return { handle: h }; });

  return ContentService.createTextOutput(JSON.stringify(queueList))
                       .setMimeType(ContentService.MimeType.JSON);
}

function doPost(e) {
  try {
    if (!e || !e.postData || !e.postData.contents) {
      return ContentService.createTextOutput(JSON.stringify({status: "error", message: "no body"}))
                           .setMimeType(ContentService.MimeType.JSON);
    }

    var params = JSON.parse(e.postData.contents);

    if (!isAuthorized_(params)) {
      return ContentService.createTextOutput(JSON.stringify({status: "error", message: "unauthorized"}))
                           .setMimeType(ContentService.MimeType.JSON);
    }

    if (params.type === 'bio') {
      return appendBioRows_(params);
    }
    return appendHasilRows_(params);
  } catch (err) {
    return ContentService.createTextOutput(JSON.stringify({status: "error", message: String(err)}))
                         .setMimeType(ContentService.MimeType.JSON);
  }
}

function appendHasilRows_(params) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheetRes = ss.getSheetByName("Hasil");

  if (!sheetRes) {
    return ContentService.createTextOutput(JSON.stringify({status: "error", message: "sheet Hasil not found"}))
                         .setMimeType(ContentService.MimeType.JSON);
  }

  var items = Array.isArray(params.items) ? params.items : [params];

  var lastRow = sheetRes.getLastRow();
  var nextNo = lastRow >= 1 ? lastRow : 1;

  var rowsToAppend = items.map(function (p) {
    var descText = p.deskripsi || p.snippet || p.description || "";
    var out = [
      nextNo,
      p.account || params.account || "",
      p.pos || "",
      p.posOverall || "",
      p.judul || "",
      p.url || "",
      p.urlShown || "",
      descText,
      p.faviconSource || "",
      p.urutanHasil || "",
      p.instagramHandle || ""
    ];
    nextNo += 1;
    return out;
  });

  if (rowsToAppend.length > 0) {
    sheetRes.getRange(
      sheetRes.getLastRow() + 1,
      1,
      rowsToAppend.length,
      rowsToAppend[0].length
    ).setValues(rowsToAppend);
  }

  return ContentService.createTextOutput(JSON.stringify({status: "success", written: rowsToAppend.length}))
                       .setMimeType(ContentService.MimeType.JSON);
}

function appendBioRows_(params) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheetBio = ss.getSheetByName("Bio");

  if (!sheetBio) {
    return ContentService.createTextOutput(JSON.stringify({status: "error", message: "sheet Bio not found -- create it first, see README"}))
                         .setMimeType(ContentService.MimeType.JSON);
  }

  var items = Array.isArray(params.items) ? params.items : [params];

  var lastRow = sheetBio.getLastRow();
  var nextNo = lastRow >= 1 ? lastRow : 1;

  var rowsToAppend = items.map(function (p) {
    var out = [
      nextNo,
      p.instagramHandle || "",
      p.namaLengkap || "",
      p.bio || "",
      p.followers || "",
      p.following || "",
      p.posts || "",
      p.website || "",
      p.isPrivate === true ? "TRUE" : (p.isPrivate === false ? "FALSE" : ""),
      p.isVerified === true ? "TRUE" : (p.isVerified === false ? "FALSE" : ""),
      p.status || ""
    ];
    nextNo += 1;
    return out;
  });

  if (rowsToAppend.length > 0) {
    sheetBio.getRange(
      sheetBio.getLastRow() + 1,
      1,
      rowsToAppend.length,
      rowsToAppend[0].length
    ).setValues(rowsToAppend);
  }

  return ContentService.createTextOutput(JSON.stringify({status: "success", written: rowsToAppend.length}))
                       .setMimeType(ContentService.MimeType.JSON);
}
