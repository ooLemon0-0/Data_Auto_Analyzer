// Paint before loading the CLR or touching the compressed runtime payload.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <commctrl.h>
#include <shlobj.h>
#include <shellapi.h>
#include <dwmapi.h>
#include <string>
#include <vector>

static HANDLE child = NULL, ready = NULL;
static bool handoff = false, testing = false;
static int result = 0;
static HINSTANCE instance;
static HFONT normalFont, titleFont;
static HBRUSH background;

static std::wstring Quote(const std::wstring& value) {
    std::wstring output = L"\"";
    unsigned slashes = 0;
    for (wchar_t ch : value) {
        if (ch == L'\\') { ++slashes; continue; }
        output.append(ch == L'"' ? slashes * 2 + 1 : slashes, L'\\');
        output.push_back(ch);
        slashes = 0;
    }
    output.append(slashes * 2, L'\\');
    return output + L"\"";
}

static HWND Add(HWND window, const wchar_t* type, const wchar_t* text, int x, int y, int w, int h, DWORD style = 0, int id = 0) {
    HWND control = CreateWindowExW(0, type, text, WS_CHILD | WS_VISIBLE | style, x, y, w, h, window, (HMENU)(INT_PTR)id, instance, NULL);
    SendMessageW(control, WM_SETFONT, (WPARAM)normalFont, TRUE);
    return control;
}

static LRESULT CALLBACK WindowProc(HWND window, UINT message, WPARAM wParam, LPARAM lParam) {
    switch (message) {
    case WM_PAINT: {
        PAINTSTRUCT paint;
        HDC dc = BeginPaint(window, &paint);
        RECT track = {30,157,470,164};
        HBRUSH rail = CreateSolidBrush(RGB(230,230,242));
        HBRUSH fill = CreateSolidBrush(RGB(102,88,232));
        FillRect(dc, &track, rail);
        int x = 30 + (GetTickCount()/20)%330;
        RECT segment = {x,157,x+100,164};
        FillRect(dc, &segment, fill);
        DrawIconEx(dc, 30, 27, LoadIconW(instance, MAKEINTRESOURCEW(2)), 33, 33, 0, NULL, DI_NORMAL);
        DeleteObject(rail); DeleteObject(fill);
        EndPaint(window, &paint);
        return 0;
    }
    case WM_CTLCOLORSTATIC:
        SetBkColor((HDC)wParam, RGB(245, 246, 250));
        SetTextColor((HDC)wParam, RGB(55, 59, 77));
        return (LRESULT)background;
    case WM_COMMAND:
        if (LOWORD(wParam) == 10) SendMessageW(window, WM_CLOSE, 0, 0);
        break;
    case WM_TIMER:
        { RECT track = {30,157,470,164}; InvalidateRect(window, &track, FALSE); }
        if (ready && WaitForSingleObject(ready, 0) == WAIT_OBJECT_0 && !handoff) {
            handoff = true;
            if (testing) ShowWindow(window, SW_HIDE);
            else { DestroyWindow(window); return 0; }
        }
        if (child && WaitForSingleObject(child, 0) == WAIT_OBJECT_0) {
            DWORD code = 1;
            GetExitCodeProcess(child, &code);
            result = (int)code;
            handoff = true;
            DestroyWindow(window);
        }
        return 0;
    case WM_CLOSE:
        if (child && !handoff) TerminateProcess(child, 1);
        DestroyWindow(window);
        return 0;
    case WM_DESTROY:
        PostQuitMessage(result);
        return 0;
    }
    return DefWindowProcW(window, message, wParam, lParam);
}

int WINAPI wWinMain(HINSTANCE module, HINSTANCE, LPWSTR command, int) {
    instance = module;
    INITCOMMONCONTROLSEX controls = { sizeof(controls), ICC_PROGRESS_CLASS };
    InitCommonControlsEx(&controls);
    background = CreateSolidBrush(RGB(245, 246, 250));
    normalFont = CreateFontW(-15, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE, DEFAULT_CHARSET, 0, 0, CLEARTYPE_QUALITY, 0, L"Microsoft YaHei UI");
    titleFont = CreateFontW(-26, 0, 0, 0, FW_BOLD, FALSE, FALSE, FALSE, DEFAULT_CHARSET, 0, 0, CLEARTYPE_QUALITY, 0, L"Microsoft YaHei UI");
    WNDCLASSW klass = {};
    klass.lpfnWndProc = WindowProc;
    klass.hInstance = instance;
    klass.hbrBackground = background;
    klass.hIcon = LoadIconW(instance, MAKEINTRESOURCEW(2));
    klass.hCursor = LoadCursorW(NULL, IDC_ARROW);
    klass.style = CS_DROPSHADOW;
    klass.lpszClassName = L"AutoAnalyzerFirstWindow";
    RegisterClassW(&klass);
    DWORD style = WS_POPUP;
    RECT rectangle = { 0, 0, 500, 245 };
    AdjustWindowRect(&rectangle, style, FALSE);
    int width = rectangle.right - rectangle.left, height = rectangle.bottom - rectangle.top;
    HWND window = CreateWindowW(klass.lpszClassName, L"数据抽样审核", style,
        (GetSystemMetrics(SM_CXSCREEN)-width)/2, (GetSystemMetrics(SM_CYSCREEN)-height)/2,
        width, height, NULL, NULL, instance, NULL);
    DWORD corners = 2;
    DwmSetWindowAttribute(window, 33, &corners, sizeof(corners));
    HWND title = Add(window, L"STATIC", L"数据抽样审核", 76, 25, 400, 45);
    SendMessageW(title, WM_SETFONT, (WPARAM)titleFont, TRUE);
    Add(window, L"STATIC", L"正在启动", 30, 87, 440, 25);
    Add(window, L"STATIC", L"正在加载界面组件", 30, 117, 440, 27);
    Add(window, L"BUTTON", L"退出", 382, 187, 88, 33, BS_PUSHBUTTON, 10);
    ShowWindow(window, SW_SHOW);
    UpdateWindow(window);
    FILETIME now;
    GetSystemTimeAsFileTime(&now);
    ULARGE_INTEGER ticks;
    ticks.LowPart = now.dwLowDateTime; ticks.HighPart = now.dwHighDateTime;
    std::wstring firstTicks = std::to_wstring(ticks.QuadPart + 504911232000000000ULL);
    SetEnvironmentVariableW(L"AUTOANALYZER_FIRST_WINDOW_TICKS", firstTicks.c_str());

    // All file I/O begins after the first frame has been painted.
    wchar_t buffer[32768];
    GetModuleFileNameW(NULL, buffer, 32768);
    std::wstring package(buffer);
    SetEnvironmentVariableW(L"AUTOANALYZER_PACKAGE_EXE", package.c_str());
    std::wstring data;
    DWORD count = GetEnvironmentVariableW(L"AUTOANALYZER_DATA_DIR", buffer, 32768);
    if (count && count < 32768) data = buffer;
    else {
        count = GetEnvironmentVariableW(L"LOCALAPPDATA", buffer, 32768);
        if (!count) SHGetFolderPathW(NULL, CSIDL_LOCAL_APPDATA, NULL, 0, buffer);
        data = std::wstring(buffer) + L"\\AutoAnalyzer";
    }
    wchar_t loaderId[80] = {};
    LoadStringW(instance, 3, loaderId, 80);
    std::wstring folder = data + L"\\startup\\" + loaderId;
    SHCreateDirectoryExW(NULL, folder.c_str(), NULL);
    std::wstring loader = folder + L"\\loader.exe";
    HRSRC resource = FindResourceW(instance, MAKEINTRESOURCEW(1), RT_RCDATA);
    DWORD size = SizeofResource(instance, resource);
    const void* bytes = LockResource(LoadResource(instance, resource));
    WIN32_FILE_ATTRIBUTE_DATA existing = {};
    if (!GetFileAttributesExW(loader.c_str(), GetFileExInfoStandard, &existing) || existing.nFileSizeLow != size) {
        std::wstring temp = loader + L"." + std::to_wstring(GetCurrentProcessId()) + L".tmp";
        HANDLE output = CreateFileW(temp.c_str(), GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
        DWORD written = 0;
        bool success = output != INVALID_HANDLE_VALUE && WriteFile(output, bytes, size, &written, NULL) && written == size;
        if (output != INVALID_HANDLE_VALUE) CloseHandle(output);
        if (!success || !MoveFileExW(temp.c_str(), loader.c_str(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
            DeleteFileW(temp.c_str());
            if (!GetFileAttributesExW(loader.c_str(), GetFileExInfoStandard, &existing) || existing.nFileSizeLow != size) {
                MessageBoxW(window, L"无法写入启动组件，请检查本机存储空间和文件权限。", L"启动失败", MB_OK | MB_ICONERROR);
                return 1;
            }
        }
    }
    std::wstring eventName = L"Local\\AutoAnalyzerPreloader_" + std::to_wstring(GetCurrentProcessId());
    ready = CreateEventW(NULL, TRUE, FALSE, eventName.c_str());
    SetEnvironmentVariableW(L"AUTOANALYZER_PRELOADER_EVENT", eventName.c_str());
    testing = wcsstr(command, L"-test") != NULL || wcsstr(command, L"--launcher-report") != NULL;
    std::wstring line = Quote(loader) + L" " + command;
    std::vector<wchar_t> mutableLine(line.begin(), line.end());
    mutableLine.push_back(0);
    STARTUPINFOW startup = { sizeof(startup) };
    PROCESS_INFORMATION process = {};
    if (!CreateProcessW(loader.c_str(), mutableLine.data(), NULL, NULL, FALSE, 0, NULL, folder.c_str(), &startup, &process)) {
        MessageBoxW(window, L"无法加载界面组件，请重新下载程序。", L"启动失败", MB_OK | MB_ICONERROR);
        return 1;
    }
    child = process.hProcess;
    CloseHandle(process.hThread);
    SetTimer(window, 1, 25, NULL);
    MSG message;
    while (GetMessageW(&message, NULL, 0, 0) > 0) {
        TranslateMessage(&message);
        DispatchMessageW(&message);
    }
    if (child) CloseHandle(child);
    if (ready) CloseHandle(ready);
    DeleteObject(normalFont); DeleteObject(titleFont); DeleteObject(background);
    return result;
}
