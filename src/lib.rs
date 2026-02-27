use std::collections::HashSet;
use std::fs::File;
use std::io::{BufRead, BufReader, Seek, SeekFrom};
use std::sync::OnceLock;

use memchr::memmem;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PySet, PyTuple};
use regex::Regex;

/// Extract plain text from an NSArchiver attributedBody blob.
///
/// Scans for b"NSString" marker, then b"\x01+", reads length byte, slices UTF-8 text.
#[pyfunction]
fn extract_attributed_body_text(py: Python<'_>, blob: &[u8]) -> PyResult<String> {
    Ok(py.allow_threads(|| {
        if blob.is_empty() {
            return String::new();
        }

        let ns_string = b"NSString";
        let finder = memmem::Finder::new(ns_string);
        let idx = match finder.find(blob) {
            Some(i) => i,
            None => return String::new(),
        };

        let search_start = idx + ns_string.len();
        let marker = b"\x01+";
        let marker_finder = memmem::Finder::new(marker);
        let plus_idx = match marker_finder.find(&blob[search_start..]) {
            Some(i) => search_start + i,
            None => return String::new(),
        };

        let length_offset = plus_idx + 2;
        if length_offset >= blob.len() {
            return String::new();
        }

        let text_len = blob[length_offset] as usize;
        let text_start = length_offset + 1;
        let text_end = text_start + text_len;
        if text_end > blob.len() {
            return String::from_utf8_lossy(&blob[text_start..]).into_owned();
        }

        String::from_utf8_lossy(&blob[text_start..text_end]).into_owned()
    }))
}

fn lsof_regex() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(
            r"^(\S+)\s+\d+\s+\S+\s+\S+\s+IPv[46]\s+\S+\s+\S+\s+TCP\s+\S+->(\d+\.\d+\.\d+\.\d+):(\d+)\s+\(ESTABLISHED\)"
        ).unwrap()
    })
}

/// Parse lsof -i -P -n output into a set of (process_name, remote_ip, remote_port) tuples.
#[pyfunction]
fn parse_lsof_output<'py>(py: Python<'py>, output: &str) -> PyResult<Bound<'py, PySet>> {
    let re = lsof_regex();

    let results: Vec<(String, String, u16)> = py.allow_threads(|| {
        let mut set = HashSet::new();
        for line in output.lines() {
            if let Some(caps) = re.captures(line) {
                let process = caps[1].to_string();
                let ip = caps[2].to_string();
                let port: u16 = caps[3].parse().unwrap_or(0);
                set.insert((process, ip, port));
            }
        }
        set.into_iter().collect()
    });

    let pyset = PySet::empty(py)?;
    for (process, ip, port) in results {
        let tuple = PyTuple::new(py, [
            process.into_pyobject(py)?.into_any(),
            ip.into_pyobject(py)?.into_any(),
            port.into_pyobject(py)?.into_any(),
        ])?;
        pyset.add(tuple)?;
    }
    Ok(pyset)
}

/// Extract text content from a user or assistant message dict.
fn extract_content(msg: &serde_json::Value) -> String {
    let content = &msg["content"];
    if let Some(s) = content.as_str() {
        return s.to_string();
    }
    if let Some(arr) = content.as_array() {
        let texts: Vec<&str> = arr
            .iter()
            .filter_map(|b| {
                if b.get("type")?.as_str()? == "text" {
                    b.get("text")?.as_str()
                } else {
                    None
                }
            })
            .collect();
        return texts.join(" ");
    }
    String::new()
}

/// Build a readable preview of a tool call input.
fn tool_input_preview(tool_name: &str, tool_input: &serde_json::Value) -> String {
    match tool_name {
        "Bash" => tool_input
            .get("command")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        "Read" | "Glob" => tool_input
            .get("file_path")
            .or_else(|| tool_input.get("pattern"))
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        "Write" => {
            let path = tool_input
                .get("file_path")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let size = tool_input
                .get("content")
                .and_then(|v| v.as_str())
                .map(|s| s.len())
                .unwrap_or(0);
            format!("{path} ({size} chars)")
        }
        "Edit" => tool_input
            .get("file_path")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        "Grep" => {
            let pattern = tool_input
                .get("pattern")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let path = tool_input
                .get("path")
                .and_then(|v| v.as_str())
                .unwrap_or(".");
            format!("/{pattern}/ in {path}")
        }
        "Task" => tool_input
            .get("description")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        _ => {
            let s = serde_json::to_string(tool_input).unwrap_or_default();
            truncate_str(&s, 200).to_string()
        }
    }
}

/// Parse ISO 8601 timestamp to epoch float. Returns None on failure.
fn parse_iso_ts(ts_str: &str) -> Option<f64> {
    // Handle "2026-02-25T08:16:18.720Z" or "2026-02-25T08:16:18.720+00:00"
    let s = ts_str.replace('Z', "+00:00");

    // Try parsing with chrono-like manual approach
    // Format: YYYY-MM-DDTHH:MM:SS.fff+HH:MM
    // We'll use a simpler approach: split at 'T', parse date and time parts

    let (date_part, rest) = s.split_once('T')?;
    let date_parts: Vec<&str> = date_part.split('-').collect();
    if date_parts.len() != 3 {
        return None;
    }
    let year: i64 = date_parts[0].parse().ok()?;
    let month: i64 = date_parts[1].parse().ok()?;
    let day: i64 = date_parts[2].parse().ok()?;

    // Split time from timezone offset
    let (time_str, tz_offset_secs) = if let Some(idx) = rest.rfind('+') {
        if idx > 0 {
            let tz_str = &rest[idx + 1..];
            let tz_parts: Vec<&str> = tz_str.split(':').collect();
            let tz_hours: i64 = tz_parts.first().and_then(|s| s.parse().ok()).unwrap_or(0);
            let tz_mins: i64 = tz_parts.get(1).and_then(|s| s.parse().ok()).unwrap_or(0);
            (&rest[..idx], tz_hours * 3600 + tz_mins * 60)
        } else {
            (rest, 0i64)
        }
    } else if let Some(idx) = rest.rfind('-') {
        // Check if this is a timezone offset (not part of date)
        // The '-' for timezone should be after the time portion
        if idx > 6 {
            let tz_str = &rest[idx + 1..];
            let tz_parts: Vec<&str> = tz_str.split(':').collect();
            let tz_hours: i64 = tz_parts.first().and_then(|s| s.parse().ok()).unwrap_or(0);
            let tz_mins: i64 = tz_parts.get(1).and_then(|s| s.parse().ok()).unwrap_or(0);
            (&rest[..idx], -(tz_hours * 3600 + tz_mins * 60))
        } else {
            (rest, 0i64)
        }
    } else {
        (rest, 0i64)
    };

    let time_parts: Vec<&str> = time_str.split(':').collect();
    if time_parts.len() < 2 {
        return None;
    }
    let hour: i64 = time_parts[0].parse().ok()?;
    let minute: i64 = time_parts[1].parse().ok()?;
    let second_str = time_parts.get(2).unwrap_or(&"0");
    let sec_parts: Vec<&str> = second_str.split('.').collect();
    let sec: i64 = sec_parts[0].parse().ok()?;
    let frac: f64 = if sec_parts.len() > 1 {
        let frac_str = sec_parts[1];
        let frac_val: f64 = frac_str.parse().ok()?;
        frac_val / 10f64.powi(frac_str.len() as i32)
    } else {
        0.0
    };

    // Convert to Unix timestamp using a simplified algorithm
    // Days from epoch to date
    let days = days_from_epoch(year, month, day)?;
    let epoch_secs = days * 86400 + hour * 3600 + minute * 60 + sec;
    Some(epoch_secs as f64 + frac - tz_offset_secs as f64)
}

/// Calculate days from Unix epoch (1970-01-01) to the given date.
fn days_from_epoch(year: i64, month: i64, day: i64) -> Option<i64> {
    // Adjust for months before March
    let (y, m) = if month <= 2 {
        (year - 1, month + 9)
    } else {
        (year, month - 3)
    };
    let era = y / 400;
    let yoe = y - era * 400;
    let doy = (153 * m + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    let days = era * 146097 + doe - 719468;
    Some(days)
}

/// Truncate a string to at most `max_len` characters.
fn truncate_str(s: &str, max_len: usize) -> &str {
    if s.len() <= max_len {
        s
    } else {
        // Find a valid char boundary
        let mut end = max_len;
        while end > 0 && !s.is_char_boundary(end) {
            end -= 1;
        }
        &s[..end]
    }
}

/// Parsed event from a transcript line.
struct TranscriptEvent {
    timestamp: f64,
    session_id: String,
    message_type: String,
    content_preview: String,
    project_path: String,
}

/// Parse a JSONL transcript file into structured events.
///
/// Returns (list_of_event_dicts, final_file_offset).
#[pyfunction]
#[pyo3(signature = (path, since_offset=0, preview_len=500))]
fn parse_transcript<'py>(
    py: Python<'py>,
    path: &str,
    since_offset: u64,
    preview_len: usize,
) -> PyResult<(Bound<'py, PyList>, u64)> {
    // Derive session_id and project_path from the path
    let file_path = std::path::Path::new(path);
    let session_id = file_path
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("")
        .to_string();
    let project_path = file_path
        .parent()
        .and_then(|p| p.to_str())
        .unwrap_or("")
        .to_string();

    let (events, final_offset) = py.allow_threads(|| -> Result<(Vec<TranscriptEvent>, u64), String> {
        let file = File::open(path).map_err(|e| e.to_string())?;
        let mut reader = BufReader::new(file);
        reader
            .seek(SeekFrom::Start(since_offset))
            .map_err(|e| e.to_string())?;

        let mut events = Vec::new();
        let mut line_buf = String::new();

        loop {
            line_buf.clear();
            let bytes_read = reader.read_line(&mut line_buf).map_err(|e| e.to_string())?;
            if bytes_read == 0 {
                break;
            }

            let trimmed = line_buf.trim();
            if trimmed.is_empty() {
                continue;
            }

            let entry: serde_json::Value = match serde_json::from_str(trimmed) {
                Ok(v) => v,
                Err(_) => continue,
            };

            let event_type = entry
                .get("type")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let ts_str = entry
                .get("timestamp")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let ts = if !ts_str.is_empty() {
                parse_iso_ts(ts_str).unwrap_or(0.0)
            } else {
                0.0 // Will be replaced with time.time() on the Python side if needed
            };

            match event_type {
                "user" => {
                    let msg = &entry["message"];
                    let content = extract_content(msg);
                    if content.trim().is_empty() {
                        continue;
                    }
                    events.push(TranscriptEvent {
                        timestamp: ts,
                        session_id: session_id.clone(),
                        message_type: "user".to_string(),
                        content_preview: truncate_str(&content, preview_len).to_string(),
                        project_path: project_path.clone(),
                    });
                }
                "assistant" => {
                    let msg = &entry["message"];
                    let content_blocks = match msg.get("content").and_then(|v| v.as_array()) {
                        Some(arr) => arr,
                        None => continue,
                    };

                    for block in content_blocks {
                        let block_type = block
                            .get("type")
                            .and_then(|v| v.as_str())
                            .unwrap_or("");

                        match block_type {
                            "text" => {
                                let text = block
                                    .get("text")
                                    .and_then(|v| v.as_str())
                                    .unwrap_or("");
                                events.push(TranscriptEvent {
                                    timestamp: ts,
                                    session_id: session_id.clone(),
                                    message_type: "assistant_text".to_string(),
                                    content_preview: truncate_str(text, preview_len).to_string(),
                                    project_path: project_path.clone(),
                                });
                            }
                            "tool_use" => {
                                let tool_name = block
                                    .get("name")
                                    .and_then(|v| v.as_str())
                                    .unwrap_or("");
                                let empty_obj = serde_json::Value::Object(serde_json::Map::new());
                                let tool_input = block
                                    .get("input")
                                    .unwrap_or(&empty_obj);
                                let preview = tool_input_preview(tool_name, tool_input);
                                events.push(TranscriptEvent {
                                    timestamp: ts,
                                    session_id: session_id.clone(),
                                    message_type: format!("tool_use:{tool_name}"),
                                    content_preview: truncate_str(&preview, preview_len)
                                        .to_string(),
                                    project_path: project_path.clone(),
                                });
                            }
                            _ => {}
                        }
                    }
                }
                "progress" => {
                    let data = &entry["data"];
                    let subtype = data
                        .get("type")
                        .and_then(|v| v.as_str())
                        .unwrap_or("");
                    if subtype == "tool_result" {
                        let tool_name = data
                            .get("tool_name")
                            .and_then(|v| v.as_str())
                            .unwrap_or("");
                        let output_str = match data.get("output") {
                            Some(v) => match v.as_str() {
                                Some(s) => s.to_string(),
                                None => v.to_string(),
                            },
                            None => String::new(),
                        };
                        events.push(TranscriptEvent {
                            timestamp: ts,
                            session_id: session_id.clone(),
                            message_type: format!("tool_result:{tool_name}"),
                            content_preview: truncate_str(&output_str, preview_len).to_string(),
                            project_path: project_path.clone(),
                        });
                    }
                }
                _ => {}
            }
        }

        let final_offset = reader.stream_position().map_err(|e| e.to_string())?;
        Ok((events, final_offset))
    })
    .map_err(|e| pyo3::exceptions::PyIOError::new_err(e))?;

    // Convert events to Python dicts
    let py_list = PyList::empty(py);
    for ev in &events {
        let dict = PyDict::new(py);
        dict.set_item("timestamp", ev.timestamp)?;
        dict.set_item("session_id", &ev.session_id)?;
        dict.set_item("message_type", &ev.message_type)?;
        dict.set_item("content_preview", &ev.content_preview)?;
        dict.set_item("project_path", &ev.project_path)?;
        py_list.append(dict)?;
    }

    Ok((py_list, final_offset))
}

#[pymodule]
fn snoopy_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(extract_attributed_body_text, m)?)?;
    m.add_function(wrap_pyfunction!(parse_lsof_output, m)?)?;
    m.add_function(wrap_pyfunction!(parse_transcript, m)?)?;
    Ok(())
}
