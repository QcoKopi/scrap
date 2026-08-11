/**
 * Roastery IG Keyword Research - Google Apps Script Web App
 * -----------------------------------------------------------
 * ONE spreadsheet, ONE deployment URL, FOUR sheet tabs:
 *   - "Keyword" : input list of search phrases
 *   - "Hasil"   : organic search results (incl. Instagram handles found)
 *   - "Bio"     : Instagram profile bio data, one row per unique handle
 *   - "Posts"   : the ~12 most recent posts per account (caption, hook,
 *                 hashtags, media type, likes/comments/views), extracted
 *                 from the SAME response used for Bio -- no extra request.
 *
 * doGet:
 *   ?action=keywordQueue (default) -> unprocessed rows from "Keyword"
 *   ?action=bioQueue              -> unique real IG handles from "Hasil"
 *                                     that don't have a "Bio" row yet
 *
 * doPost body:
 *   { ...,              "items": [...] }              -> appends to "Hasil" (default)
 *   { "type": "bio",    "items": [...] }               -> appends to "Bio"
 *   { "type": "posts",  "items": [...] }               -> appends to "Posts"
 *
 * Auth: optional shared-secret token (Script Property SHARED_TOKEN), see
 * isAuthorized_(). Skipped entirely if the property isn't set.
 */

var IG_PLACEHOLDER_HANDLES = ["@reel", "@p", "@instagram", ""];
var BIO_HEADERS = ["No", "Account ID", "Instagram Handle", "Nama Lengkap", "Bio", "Followers", "Following", "Posts", "Website", "Private", "Verified", "Status"];
var POSTS_HEADERS = ["No", "Account ID", "Instagram Handle", "Tipe Media", "URL Post", "Hook (Baris Pertama)", "Caption Lengkap", "Hashtag", "Tanggal Post", "Likes", "Comments", "Views"];

function isAuthorized_(params) {
  var expected = PropertiesService.getScriptProperties().getProperty('SHARED_TOKEN');
  if (!expected) return true; // no token configured -> open (legacy behavior)
  return params && params.token === expected;
}

function isRealHandle_(handle) {
  if (!handle) return false;
  return IG_PLACEHOLDER_HANDLES.indexOf(handle) === -1;
}

// Deterministic short ID per Instagram handle -- same handle always maps to
// the same 8-char ID, computed independently wherever it's needed (Hasil
// rows, Bio rows) with no shared lookup table to keep in sync. This is what
// lets one account (1 handle) be joined across many Hasil rows via a stable
// key, instead of only the raw handle text.
function computeAccountId_(handle) {
  if (!handle) return "";
  var digestBytes = Utilities.computeDigest(Utilities.DigestAlgorithm.MD5, handle, Utilities.Charset.UTF_8);
  var hex = digestBytes.map(function (b) {
    var v = (b < 0 ? b + 256 : b).toString(16);
    return v.length === 1 ? "0" + v : v;
  }).join("");
  return hex.substring(0, 8).toUpperCase();
}

// Individual post/reel URLs (instagram.com/p/<shortcode>/) don't contain the
// poster's username at all -- an Instagram URL-structure limitation, not
// something fixable from the link alone. This looks for an @mention in the
// post's title/snippet text instead (e.g. "Dari @kopikita.jkt di Jakarta").
// Mirrors extract_mention_fallback() in src/auto_pipeline.py -- keep both in
// sync if this regex ever changes.
var IG_MENTION_RE = /@[\w.]{2,30}/g;

function extractMentionFallback_(text) {
  if (!text) return "";
  var matches = text.match(IG_MENTION_RE);
  if (!matches) return "";
  for (var i = 0; i < matches.length; i++) {
    var cleaned = matches[i].replace(/[.,;:!?)]+$/, "");
    if (cleaned.length > 1) return cleaned;
  }
  return "";
}

// Creates the "Bio" sheet tab with the correct header row if it doesn't
// exist yet, so nobody has to set it up by hand (and risk a typo'd header
// that silently breaks column alignment).
function getOrCreateBioSheet_(ss) {
  var sheet = ss.getSheetByName("Bio");
  if (!sheet) {
    sheet = ss.insertSheet("Bio");
    sheet.getRange(1, 1, 1, BIO_HEADERS.length).setValues([BIO_HEADERS]);
    sheet.setFrozenRows(1);
  }
  return sheet;
}

// Same auto-create pattern for "Posts".
function getOrCreatePostsSheet_(ss) {
  var sheet = ss.getSheetByName("Posts");
  if (!sheet) {
    sheet = ss.insertSheet("Posts");
    sheet.getRange(1, 1, 1, POSTS_HEADERS.length).setValues([POSTS_HEADERS]);
    sheet.setFrozenRows(1);
  }
  return sheet;
}

// "Hasil" already has a fixed 11-column layout the user set up manually, so
// Account ID is added as a new column L at the end (not inserted in the
// middle) to avoid disturbing existing data/columns. Header is added
// automatically if missing.
function ensureHasilAccountIdHeader_(sheetRes) {
  var headerCell = sheetRes.getRange(1, 12);
  if (!headerCell.getValue()) {
    headerCell.setValue("Account ID");
  }
}

function doGet(e) {
  try {
    var action = (e && e.parameter && e.parameter.action) || 'keywordQueue';
    if (action === 'bioQueue') {
      return bioQueueResponse_();
    }
    if (action === 'backfillAccountIds') {
      return backfillAccountIdsResponse_();
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

// One-time (idempotent, safe to re-run) cleanup pass for rows written
// before the Account ID / mention-recovery features existed. Visit this URL
// directly in a browser -- GAS_WEB_APP_URL + "?action=backfillAccountIds" --
// no Python/GitHub Actions run needed, no Oxylabs calls (pure text
// processing on data already in the sheet). Uses batched range read/write
// (not per-cell) so it stays fast even at thousands of rows.
//
// Does two things per Hasil row:
//   1. If Instagram Handle is still a placeholder (@p/@reel/@instagram),
//      try to recover the real handle from an @mention in Judul/Deskripsi.
//   2. If the (possibly just-recovered) handle is real and Account ID is
//      blank, compute and fill it.
function backfillAccountIdsResponse_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheetRes = ss.getSheetByName("Hasil");
  var sheetBio = ss.getSheetByName("Bio");
  var updated = { hasil: 0, bio: 0, handlesRecovered: 0 };

  if (sheetRes && sheetRes.getLastRow() > 1) {
    ensureHasilAccountIdHeader_(sheetRes);
    var numRows = sheetRes.getLastRow() - 1;
    var handles = sheetRes.getRange(2, 11, numRows, 1).getValues(); // col K
    var ids = sheetRes.getRange(2, 12, numRows, 1).getValues();     // col L
    var judul = sheetRes.getRange(2, 5, numRows, 1).getValues();    // col E
    var deskripsi = sheetRes.getRange(2, 8, numRows, 1).getValues(); // col H

    var newHandles = [];
    var newIds = [];
    for (var i = 0; i < numRows; i++) {
      var handle = handles[i][0];
      var currentId = ids[i][0];

      if (!isRealHandle_(handle)) {
        var recovered = extractMentionFallback_(
          (judul[i][0] || "") + " " + (deskripsi[i][0] || "")
        );
        if (recovered) {
          handle = recovered;
          updated.handlesRecovered++;
        }
      }
      newHandles.push([handle]);

      if (isRealHandle_(handle) && !currentId) {
        updated.hasil++;
        newIds.push([computeAccountId_(handle)]);
      } else {
        newIds.push([currentId]);
      }
    }
    sheetRes.getRange(2, 11, numRows, 1).setValues(newHandles);
    sheetRes.getRange(2, 12, numRows, 1).setValues(newIds);
  }

  if (sheetBio && sheetBio.getLastRow() > 1) {
    var numRowsBio = sheetBio.getLastRow() - 1;
    var handlesBio = sheetBio.getRange(2, 3, numRowsBio, 1).getValues(); // col C
    var idsBio = sheetBio.getRange(2, 2, numRowsBio, 1).getValues();     // col B
    var newIdsBio = idsBio.map(function (row, i) {
      var handle = handlesBio[i][0];
      var currentId = row[0];
      if (handle && !currentId) {
        updated.bio++;
        return [computeAccountId_(handle)];
      }
      return [currentId];
    });
    sheetBio.getRange(2, 2, numRowsBio, 1).setValues(newIdsBio);
  }

  return ContentService.createTextOutput(JSON.stringify({status: "success", updated: updated}))
                       .setMimeType(ContentService.MimeType.JSON);
}

function bioQueueResponse_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheetRes = ss.getSheetByName("Hasil");

  if (!sheetRes) {
    return ContentService.createTextOutput(JSON.stringify([]))
                         .setMimeType(ContentService.MimeType.JSON);
  }

  var sheetBio = getOrCreateBioSheet_(ss);

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
  var dataBio = sheetBio.getDataRange().getValues();
  for (var k = 1; k < dataBio.length; k++) {
    // Column C (index 2) = "Instagram Handle" in the Bio sheet layout:
    // No, Account ID, Instagram Handle, ...
    if (dataBio[k] && dataBio[k][2]) {
      alreadyDone[dataBio[k][2]] = true;
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
    if (params.type === 'posts') {
      return appendPostRows_(params);
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

  ensureHasilAccountIdHeader_(sheetRes);

  var items = Array.isArray(params.items) ? params.items : [params];

  var lastRow = sheetRes.getLastRow();
  var nextNo = lastRow >= 1 ? lastRow : 1;

  var rowsToAppend = items.map(function (p) {
    var descText = p.deskripsi || p.snippet || p.description || "";
    var handle = p.instagramHandle || "";
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
      handle,
      isRealHandle_(handle) ? computeAccountId_(handle) : ""
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
  var sheetBio = getOrCreateBioSheet_(ss);

  var items = Array.isArray(params.items) ? params.items : [params];

  var lastRow = sheetBio.getLastRow();
  var nextNo = lastRow >= 1 ? lastRow : 1;

  var rowsToAppend = items.map(function (p) {
    var handle = p.instagramHandle || "";
    var out = [
      nextNo,
      computeAccountId_(handle),
      handle,
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

function appendPostRows_(params) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheetPosts = getOrCreatePostsSheet_(ss);

  var items = Array.isArray(params.items) ? params.items : [params];

  var lastRow = sheetPosts.getLastRow();
  var nextNo = lastRow >= 1 ? lastRow : 1;

  var rowsToAppend = items.map(function (p) {
    var handle = p.instagramHandle || "";
    var out = [
      nextNo,
      computeAccountId_(handle),
      handle,
      p.mediaType || "",
      p.postUrl || "",
      p.hook || "",
      p.caption || "",
      p.hashtags || "",
      p.postedAt || "",
      p.likes !== undefined && p.likes !== null ? p.likes : "",
      p.comments !== undefined && p.comments !== null ? p.comments : "",
      p.views !== undefined && p.views !== null ? p.views : ""
    ];
    nextNo += 1;
    return out;
  });

  if (rowsToAppend.length > 0) {
    sheetPosts.getRange(
      sheetPosts.getLastRow() + 1,
      1,
      rowsToAppend.length,
      rowsToAppend[0].length
    ).setValues(rowsToAppend);
  }

  return ContentService.createTextOutput(JSON.stringify({status: "success", written: rowsToAppend.length}))
                       .setMimeType(ContentService.MimeType.JSON);
}
