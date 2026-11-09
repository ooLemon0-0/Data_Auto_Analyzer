// Small Windows startup UI. The Python/Qt payload is opened only after Shown.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

internal static class Launcher {
    internal static readonly Stopwatch Clock = Stopwatch.StartNew();
    internal static int Result;
    [STAThread] static int Main(string[] args) {
        Clock.Restart();
        AppContext.SetSwitch("Switch.System.IO.UseLegacyPathHandling", false);
        AppContext.SetSwitch("Switch.System.IO.BlockLongPaths", false);
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new LoadingForm(args));
        return Result;
    }
}

internal sealed class LoadingForm : Form {
    readonly Label status = new Label(), detail = new Label();
    readonly ProgressBar progress = new ProgressBar();
    readonly Button exit = new Button();
    readonly CancellationTokenSource cancellation = new CancellationTokenSource();
    readonly System.Windows.Forms.Timer monitor = new System.Windows.Forms.Timer();
    readonly List<string> arguments = new List<string>();
    readonly string report;
    readonly bool testMode;
    Process child;
    EventWaitHandle visible;
    bool handoff, ending, extracted;
    long shownMs = -1, runtimeMs = -1, visibleMs = -1;
    long shownUtcTicks;
    string error = "";
    string cacheStatus = "checking", startupReport;

    internal LoadingForm(string[] args) {
        for (int i = 0; i < args.Length; i++) {
            if (args[i] == "--launcher-report" && i + 1 < args.Length) report = Path.GetFullPath(args[++i]);
            else arguments.Add(args[i]);
        }
        testMode = report != null || arguments.Any(a => a.EndsWith("-test"));
        Text = "数据抽样审核";
        ClientSize = new Size(500, 245);
        FormBorderStyle = FormBorderStyle.None;
        MaximizeBox = false;
        StartPosition = FormStartPosition.CenterScreen;
        BackColor = Color.FromArgb(245, 246, 250);
        Font = new Font("Microsoft YaHei UI", 9F);
        using (var icon = System.Reflection.Assembly.GetExecutingAssembly().GetManifestResourceStream("AppIcon")) {
            if (icon != null) Icon = new Icon(icon);
        }
        try {
            Controls.Add(new PictureBox { Image = Icon.ToBitmap(), SizeMode = PictureBoxSizeMode.Zoom, Location = new Point(30, 27), Size = new Size(33, 33) });
        } catch (ArgumentException) {
            Controls.Add(new Panel { BackColor = Color.FromArgb(102,88,232), Location = new Point(30,27), Size = new Size(33,33) });
        }
        var title = new Label { Text = "数据抽样审核", AutoSize = true,
            Font = new Font("Microsoft YaHei UI", 19F, FontStyle.Bold), Location = new Point(76, 25) };
        Controls.Add(title);
        status.Text = "正在启动";
        status.SetBounds(30, 87, 440, 25);
        status.ForeColor = Color.FromArgb(55, 59, 77);
        Controls.Add(status);
        detail.Text = "正在准备运行文件";
        detail.SetBounds(30, 117, 440, 27);
        detail.ForeColor = Color.FromArgb(116, 123, 143);
        Controls.Add(detail);
        progress.SetBounds(30, 157, 440, 7);
        progress.Style = ProgressBarStyle.Marquee;
        Controls.Add(progress);
        exit.Text = "退出";
        exit.SetBounds(382, 187, 88, 33);
        exit.FlatStyle = FlatStyle.Flat;
        exit.FlatAppearance.BorderColor = Color.FromArgb(218, 221, 235);
        exit.BackColor = Color.White;
        exit.Click += delegate { Close(); };
        Controls.Add(exit);
        monitor.Interval = 50;
        monitor.Tick += Monitor;
        Shown += delegate {
            shownMs = Launcher.Clock.ElapsedMilliseconds;
            shownUtcTicks = DateTime.UtcNow.Ticks;
            long earlier;
            if (Int64.TryParse(Environment.GetEnvironmentVariable("AUTOANALYZER_FIRST_WINDOW_TICKS"), out earlier)) shownUtcTicks = earlier;
            string preloaderEvent = Environment.GetEnvironmentVariable("AUTOANALYZER_PRELOADER_EVENT");
            if (!String.IsNullOrEmpty(preloaderEvent)) {
                using (var handle = EventWaitHandle.OpenExisting(preloaderEvent)) handle.Set();
            }
            WriteReport();
            if (report != null) {
                try {
                    using (var bitmap = new Bitmap(Width, Height)) {
                        DrawToBitmap(bitmap, new Rectangle(0, 0, Width, Height));
                        bitmap.Save(Path.ChangeExtension(report, ".png"));
                    }
                } catch (Exception) { /* Diagnostic capture must not stop startup. */ }
            }
            Task.Run((Action)StartCore);
        };
        FormClosing += delegate {
            ending = true;
            cancellation.Cancel();
            if (!handoff && child != null) {
                try { if (!child.HasExited) child.Kill(); } catch { }
            }
            WriteReport();
        };
    }

    void Post(string message, string description, int percent) {
        if (ending || IsDisposed) return;
        try {
            BeginInvoke((Action)delegate {
                if (ending) return;
                status.Text = message;
                detail.Text = description;
                progress.Style = percent < 0 ? ProgressBarStyle.Marquee : ProgressBarStyle.Continuous;
                if (percent >= 0) progress.Value = Math.Max(0, Math.Min(100, percent));
            });
        } catch (InvalidOperationException) { }
    }

    static string Hex(byte[] bytes) { return BitConverter.ToString(bytes).Replace("-", "").ToLowerInvariant(); }
    static string FilePath(string path) {
        path = path.Replace('/', '\\');
        if (path.StartsWith(@"\\?\")) return path;
        return path.StartsWith(@"\\") ? @"\\?\UNC\" + path.Substring(2) : @"\\?\" + path;
    }
    static bool RuntimeComplete(string root, string digest) {
        string marker = Path.Combine(root, ".complete");
        string[] required = { "AutoAnalyzerCore.exe",
            "_internal/PySide6/QtCore.pyd", "_internal/PySide6/QtWidgets.pyd",
            "_internal/desktop_assets/config.json", "_internal/desktop_assets/chromium/chrome.exe" };
        return Directory.Exists(FilePath(Path.Combine(root, "_internal"))) && Directory.GetFiles(FilePath(Path.Combine(root, "_internal")), "python3*.dll").Length > 0 &&
            File.Exists(FilePath(marker)) && File.ReadAllText(FilePath(marker)) == digest &&
            required.All(name => File.Exists(FilePath(Path.Combine(root, name))) && new FileInfo(FilePath(Path.Combine(root, name))).Length > 0);
    }

    void StartCore() {
        try {
            string self = Environment.GetEnvironmentVariable("AUTOANALYZER_PACKAGE_EXE") ?? Application.ExecutablePath;
            string data = Environment.GetEnvironmentVariable("AUTOANALYZER_DATA_DIR");
            if (String.IsNullOrEmpty(data)) {
                string local = Environment.GetEnvironmentVariable("LOCALAPPDATA");
                if (String.IsNullOrEmpty(local)) local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
                data = Path.Combine(local, "AutoAnalyzer");
            }
            data = Path.GetFullPath(data);
            startupReport = Path.Combine(data, "logs", "startup-last.json");
            string runtime;
            using (var binary = new FileStream(self, FileMode.Open, FileAccess.Read, FileShare.Read)) {
                if (binary.Length < 48) throw new InvalidDataException("启动文件不完整，请重新下载程序。");
                binary.Position = binary.Length - 48;
                var reader = new BinaryReader(binary);
                if (Encoding.ASCII.GetString(reader.ReadBytes(8)) != "AARUN001") throw new InvalidDataException("启动文件不完整，请重新下载程序。");
                long length = reader.ReadInt64();
                byte[] expected = reader.ReadBytes(32);
                if (length <= 0 || length > binary.Length - 48) throw new InvalidDataException("运行文件大小无效，请重新下载程序。");
                string digest = Hex(expected);
                runtime = Path.Combine(data, "app-runtime", digest.Substring(0, 32));
                using (var mutex = new Mutex(false, "Local\\AutoAnalyzerRuntime_" + digest)) {
                    bool acquired = false;
                    try {
                        Post("检查运行文件", "正在检查本机已安装的运行文件", -1);
                        while (!acquired) {
                            cancellation.Token.ThrowIfCancellationRequested();
                            try { acquired = mutex.WaitOne(100); } catch (AbandonedMutexException) { acquired = true; }
                        }
                        if (!RuntimeComplete(runtime, digest)) {
                            bool priorInstall = Directory.Exists(FilePath(Path.Combine(data, "app-runtime"))) &&
                                Directory.GetDirectories(FilePath(Path.Combine(data, "app-runtime"))).Any(folder => File.Exists(FilePath(Path.Combine(folder, ".complete"))));
                            cacheStatus = Directory.Exists(FilePath(runtime)) ? "repair" : priorInstall ? "update" : "first";
                            string prepareTitle = cacheStatus == "repair" ? "修复本地组件" : cacheStatus == "update" ? "准备新版组件" : "准备程序组件";
                            using (var payload = new PayloadStream(binary, binary.Length - 48 - length, length)) {
                                Post("校验运行文件", "正在校验程序内置文件", 0);
                                using (var sha = SHA256.Create()) {
                                    byte[] buffer = new byte[1048576];
                                    long read = 0;
                                    int count;
                                    while ((count = payload.Read(buffer, 0, buffer.Length)) > 0) {
                                        cancellation.Token.ThrowIfCancellationRequested();
                                        sha.TransformBlock(buffer, 0, count, null, 0);
                                        read += count;
                                        Post("校验运行文件", String.Format("{0:F1} / {1:F1} MB", read / 1048576.0, length / 1048576.0), (int)(read * 100 / length));
                                    }
                                    sha.TransformFinalBlock(new byte[0], 0, 0);
                                    if (Hex(sha.Hash) != digest) throw new InvalidDataException("运行文件校验失败，请重新下载程序。");
                                }
                                payload.Position = 0;
                                using (var archive = new ZipArchive(payload, ZipArchiveMode.Read, true)) {
                                    string prefix = Path.GetFullPath(runtime) + Path.DirectorySeparatorChar;
                                    int completed = 0;
                                    foreach (var entry in archive.Entries) {
                                        cancellation.Token.ThrowIfCancellationRequested();
                                        string target = Path.GetFullPath(Path.Combine(runtime, entry.FullName.Replace('/', Path.DirectorySeparatorChar)));
                                        if (!target.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("运行文件路径无效。");
                                        if (entry.FullName.EndsWith("/")) { Directory.CreateDirectory(FilePath(target)); continue; }
                                        Directory.CreateDirectory(FilePath(Path.GetDirectoryName(target)));
                                        using (var source = entry.Open())
                                        using (var output = new FileStream(FilePath(target), FileMode.Create, FileAccess.Write, FileShare.None)) {
                                            byte[] buffer = new byte[1048576];
                                            int count;
                                            while ((count = source.Read(buffer, 0, buffer.Length)) > 0) {
                                                cancellation.Token.ThrowIfCancellationRequested();
                                                output.Write(buffer, 0, count);
                                            }
                                        }
                                        completed++;
                                        Post(prepareTitle, String.Format("从本地程序解压，无需下载：{0} / {1} 个文件", completed, archive.Entries.Count), completed * 100 / archive.Entries.Count);
                                    }
                                }
                            }
                            File.WriteAllText(FilePath(Path.Combine(runtime, ".complete")), digest);
                            extracted = true;
                        } else cacheStatus = "cached";
                    } finally { if (acquired) mutex.ReleaseMutex(); }
                }
            }
            cancellation.Token.ThrowIfCancellationRequested();
            runtimeMs = Launcher.Clock.ElapsedMilliseconds;
            Post("加载界面", "正在启动图片审核程序", -1);
            string eventName = "Local\\AutoAnalyzerVisible_" + Guid.NewGuid().ToString("N");
            visible = new EventWaitHandle(false, EventResetMode.ManualReset, eventName);
            var info = new ProcessStartInfo(Path.Combine(runtime, "AutoAnalyzerCore.exe"));
            info.UseShellExecute = false;
            info.WorkingDirectory = runtime;
            info.Arguments = String.Join(" ", arguments.Select(Quote));
            info.EnvironmentVariables["AUTOANALYZER_LAUNCHER_EXE"] = self;
            info.EnvironmentVariables["AUTOANALYZER_VISIBLE_EVENT"] = eventName;
            // Developer environment variables must not alter the bundled runtime.
            info.EnvironmentVariables.Remove("PYTHONHOME");
            info.EnvironmentVariables.Remove("PYTHONPATH");
            child = Process.Start(info);
            if (ending) { try { child.Kill(); } catch { } return; }
            BeginInvoke((Action)delegate { monitor.Start(); });
        } catch (OperationCanceledException) { }
        catch (Exception ex) {
            error = ex.Message;
            Launcher.Result = 1;
            Post("启动失败", ex.Message, 0);
            WriteReport();
            if (testMode && !ending) { try { BeginInvoke((Action)delegate { Close(); }); } catch { } }
        }
    }

    void Monitor(object sender, EventArgs e) {
        if (!handoff && visible != null && visible.WaitOne(0)) {
            handoff = true;
            visibleMs = Launcher.Clock.ElapsedMilliseconds;
            WriteReport();
            if (!testMode) { Close(); return; }
            Hide();
        }
        if (child != null && child.HasExited) {
            handoff = true;
            Launcher.Result = child.ExitCode;
            if (child.ExitCode != 0) error = "审核程序启动失败（" + child.ExitCode + "），请重新打开程序。";
            WriteReport();
            if (child.ExitCode == 0 || testMode) Close();
            else { monitor.Stop(); Show(); Post("启动失败", error, 0); }
        }
    }

    void WriteReport() {
        string destination = report ?? startupReport;
        if (destination == null) return;
        try {
            Directory.CreateDirectory(Path.GetDirectoryName(destination));
            string json = String.Format("{{\"shown_ms\":{0},\"runtime_ready_ms\":{1},\"qt_visible_ms\":{2},\"extracted\":{3},\"exit_code\":{4},\"error\":\"{5}\",\"shown_utc_ticks\":{6},\"cache_status\":\"{7}\"}}",
                shownMs, runtimeMs, visibleMs, extracted ? "true" : "false", Launcher.Result, error.Replace("\\", "\\\\").Replace("\"", "\\\"").Replace("\r", "").Replace("\n", " "), shownUtcTicks, cacheStatus);
            File.WriteAllText(destination, json, Encoding.UTF8);
        } catch { }
    }

    static string Quote(string argument) {
        var text = new StringBuilder("\"");
        int slashes = 0;
        foreach (char character in argument) {
            if (character == '\\') { slashes++; continue; }
            if (character == '"') { text.Append('\\', slashes * 2 + 1); text.Append('"'); slashes = 0; continue; }
            text.Append('\\', slashes); slashes = 0; text.Append(character);
        }
        text.Append('\\', slashes * 2); text.Append('"');
        return text.ToString();
    }
}

internal sealed class PayloadStream : Stream {
    readonly Stream source;
    readonly long start, length;
    long position;
    internal PayloadStream(Stream source, long start, long length) { this.source = source; this.start = start; this.length = length; }
    public override bool CanRead { get { return true; } }
    public override bool CanSeek { get { return true; } }
    public override bool CanWrite { get { return false; } }
    public override long Length { get { return length; } }
    public override long Position { get { return position; } set { Seek(value, SeekOrigin.Begin); } }
    public override int Read(byte[] buffer, int offset, int count) {
        source.Position = start + position;
        int read = source.Read(buffer, offset, (int)Math.Min(count, length - position));
        position += read; return read;
    }
    public override long Seek(long offset, SeekOrigin origin) {
        long next = origin == SeekOrigin.Begin ? offset : origin == SeekOrigin.Current ? position + offset : length + offset;
        if (next < 0 || next > length) throw new IOException("Invalid archive seek");
        return position = next;
    }
    public override void Flush() { }
    public override void SetLength(long value) { throw new NotSupportedException(); }
    public override void Write(byte[] buffer, int offset, int count) { throw new NotSupportedException(); }
}
