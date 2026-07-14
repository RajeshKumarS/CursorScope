#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    net::TcpStream,
    process::{Child, Command, Stdio},
    sync::Mutex,
    time::Duration,
};

use tauri::RunEvent;

struct ApiServer(Mutex<Option<Child>>);

fn spawn_api_server() -> Result<Child, String> {
    let python = if cfg!(windows) { "python" } else { "python3" };
    let child = Command::new(python)
        .args(["-m", "cursorscope", "serve", "--host", "127.0.0.1", "--port", "8765"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|err| format!("failed to start cursorscope serve: {err}"))?;
    Ok(child)
}

fn wait_for_api() {
    for _ in 0..120 {
        if TcpStream::connect("127.0.0.1:8765").is_ok() {
            return;
        }
        std::thread::sleep(Duration::from_millis(250));
    }
}

fn stop_server(state: &ApiServer) {
    if let Ok(mut guard) = state.0.lock() {
        if let Some(mut child) = guard.take() {
            let _ = child.kill();
        }
    }
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            let child = spawn_api_server()?;
            wait_for_api();
            app.manage(ApiServer(Mutex::new(Some(child))));
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                if let Some(state) = window.app_handle().try_state::<ApiServer>() {
                    stop_server(&state);
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            if let RunEvent::Exit = event {
                if let Some(state) = app_handle.try_state::<ApiServer>() {
                    stop_server(&state);
                }
            }
        });
}
