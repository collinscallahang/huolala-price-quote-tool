param(
  [string]$OutputExe = ""
)

$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$RootPath = $Root.Path
if ([string]::IsNullOrWhiteSpace($OutputExe)) {
  $OutputExe = Join-Path $RootPath "HuolalaPriceQuoteTool.exe"
}

$Csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (!(Test-Path -LiteralPath $Csc)) {
  $Csc = Join-Path $env:WINDIR "Microsoft.NET\Framework\v4.0.30319\csc.exe"
}
if (!(Test-Path -LiteralPath $Csc)) {
  throw "Missing .NET Framework C# compiler (csc.exe)."
}

$BuildDir = Join-Path $RootPath ".tmp\bootstrapper_build"
$SourcePath = Join-Path $BuildDir "HuolalaPriceQuoteTool.cs"
New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null

$Source = @'
using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Net;
using System.Text;
using System.Threading;
using System.Windows.Forms;

public sealed class LauncherForm : Form
{
    private const string VersionUrl = "https://raw.githubusercontent.com/collinscallahang/huolala-price-quote-tool/main/VERSION";
    private const string DownloadUrl = "https://github.com/collinscallahang/huolala-price-quote-tool/raw/main/releases/huolala-price-quote-tool-portable-latest.zip";

    private readonly Label statusLabel;
    private readonly string appHome;
    private readonly string appDir;
    private readonly string zipPath;
    private readonly string stagingDir;
    private readonly string backupDir;

    public LauncherForm()
    {
        Text = "批量查价工具";
        Width = 560;
        Height = 220;
        StartPosition = FormStartPosition.CenterScreen;
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;

        statusLabel = new Label();
        statusLabel.Left = 22;
        statusLabel.Top = 24;
        statusLabel.Width = 500;
        statusLabel.Height = 110;
        statusLabel.Font = new System.Drawing.Font("Microsoft YaHei UI", 10);
        statusLabel.Text = "正在准备查价工具...";
        Controls.Add(statusLabel);

        var hint = new Label();
        hint.Left = 22;
        hint.Top = 142;
        hint.Width = 500;
        hint.Height = 32;
        hint.ForeColor = System.Drawing.Color.FromArgb(90, 100, 120);
        hint.Font = new System.Drawing.Font("Microsoft YaHei UI", 8);
        hint.Text = "首次启动会下载运行包。请不要关闭此窗口。";
        Controls.Add(hint);

        appHome = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "HuolalaPriceQuoteTool"
        );
        appDir = Path.Combine(appHome, "app");
        zipPath = Path.Combine(appHome, "huolala-price-quote-tool-portable-latest.zip");
        stagingDir = Path.Combine(appHome, "staging");
        backupDir = Path.Combine(appHome, "user-data-backup");
    }

    protected override void OnShown(EventArgs e)
    {
        base.OnShown(e);
        ThreadPool.QueueUserWorkItem(delegate { RunLauncher(); });
    }

    private void SetStatus(string text)
    {
        if (IsDisposed) return;
        BeginInvoke((Action)(delegate { statusLabel.Text = text; }));
    }

    private void RunLauncher()
    {
        try
        {
            ServicePointManager.SecurityProtocol = (SecurityProtocolType)3072;
            Directory.CreateDirectory(appHome);

            SetStatus("正在检查最新版本...");
            string remoteVersion = DownloadText(VersionUrl).Trim();
            if (string.IsNullOrWhiteSpace(remoteVersion))
            {
                throw new Exception("远端版本号为空。");
            }

            string localVersion = ReadVersion(Path.Combine(appDir, "VERSION"));
            if (!IsUsableApp() || !string.Equals(localVersion, remoteVersion, StringComparison.OrdinalIgnoreCase))
            {
                InstallPackage(remoteVersion);
            }

            SetStatus("正在启动本地服务...");
            StartServer();
            string url = WaitForServer(remoteVersion);

            SetStatus("已打开查价网页。\r\n" + url);
            Process.Start(url);
            Thread.Sleep(1500);
            BeginInvoke((Action)(Close));
        }
        catch (Exception ex)
        {
            SetStatus("启动失败。");
            MessageBox.Show(
                "查价工具启动失败：\r\n\r\n" + ex.Message + "\r\n\r\n安装目录：\r\n" + appHome,
                "批量查价工具",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error
            );
        }
    }

    private string DownloadText(string url)
    {
        using (var client = new WebClient())
        {
            client.Encoding = Encoding.UTF8;
            return client.DownloadString(url + "?t=" + DateTimeOffset.Now.ToUnixTimeMilliseconds());
        }
    }

    private void InstallPackage(string version)
    {
        SetStatus("正在下载新版本 " + version + "...\r\n这一步可能需要几分钟。");
        using (var client = new WebClient())
        {
            client.DownloadFile(DownloadUrl + "?t=" + DateTimeOffset.Now.ToUnixTimeMilliseconds(), zipPath);
        }

        if (Directory.Exists(stagingDir)) Directory.Delete(stagingDir, true);
        Directory.CreateDirectory(stagingDir);

        SetStatus("正在解压运行包...");
        ZipFile.ExtractToDirectory(zipPath, stagingDir);

        if (!File.Exists(Path.Combine(stagingDir, "启动查价工具.bat")) ||
            !File.Exists(Path.Combine(stagingDir, "VERSION")) ||
            !File.Exists(Path.Combine(stagingDir, "runtime", "python", "python.exe")))
        {
            throw new Exception("运行包不完整，请重新下载。");
        }

        if (Directory.Exists(appDir))
        {
            SaveUserData();
            Directory.Delete(appDir, true);
        }

        Directory.Move(stagingDir, appDir);
        RestoreUserData();
    }

    private bool IsUsableApp()
    {
        return File.Exists(Path.Combine(appDir, "runtime", "python", "python.exe")) &&
            File.Exists(Path.Combine(appDir, "src", "price_quote_tool", "server.py"));
    }

    private string ReadVersion(string path)
    {
        return File.Exists(path) ? File.ReadAllText(path, Encoding.UTF8).Trim() : "";
    }

    private void SaveUserData()
    {
        if (Directory.Exists(backupDir)) Directory.Delete(backupDir, true);
        Directory.CreateDirectory(backupDir);
        foreach (string relative in new string[] { "data\\browser-profile", "data\\browser-storage-state.json", "input", "output", "outputs" })
        {
            string source = Path.Combine(appDir, relative);
            if (!Directory.Exists(source) && !File.Exists(source)) continue;
            string target = Path.Combine(backupDir, relative);
            Directory.CreateDirectory(Path.GetDirectoryName(target));
            if (Directory.Exists(source)) Directory.Move(source, target);
            else File.Move(source, target);
        }
    }

    private void RestoreUserData()
    {
        if (!Directory.Exists(backupDir)) return;
        foreach (string relative in new string[] { "data\\browser-profile", "data\\browser-storage-state.json", "input", "output", "outputs" })
        {
            string source = Path.Combine(backupDir, relative);
            if (!Directory.Exists(source) && !File.Exists(source)) continue;
            string target = Path.Combine(appDir, relative);
            Directory.CreateDirectory(Path.GetDirectoryName(target));
            if (Directory.Exists(target)) Directory.Delete(target, true);
            if (File.Exists(target)) File.Delete(target);
            if (Directory.Exists(source)) Directory.Move(source, target);
            else File.Move(source, target);
        }
        Directory.Delete(backupDir, true);
    }

    private void StartServer()
    {
        string outputsDir = Path.Combine(appDir, "outputs");
        Directory.CreateDirectory(outputsDir);
        string urlFile = Path.Combine(outputsDir, "server_url.txt");
        if (File.Exists(urlFile)) File.Delete(urlFile);

        var psi = new ProcessStartInfo();
        psi.FileName = Path.Combine(appDir, "runtime", "python", "python.exe");
        psi.Arguments = "-m price_quote_tool.server";
        psi.WorkingDirectory = appDir;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        psi.EnvironmentVariables["PRICE_QUOTE_NO_BROWSER"] = "1";
        psi.EnvironmentVariables["PYTHONPATH"] = Path.Combine(appDir, "src");
        Process.Start(psi);
    }

    private string WaitForServer(string expectedVersion)
    {
        string urlFile = Path.Combine(appDir, "outputs", "server_url.txt");
        for (int i = 0; i < 90; i++)
        {
            if (File.Exists(urlFile))
            {
                string url = File.ReadAllText(urlFile, Encoding.UTF8).Trim();
                if (!string.IsNullOrWhiteSpace(url) && IsExpectedServer(url, expectedVersion))
                {
                    return url;
                }
            }
            Thread.Sleep(1000);
        }
        throw new Exception("本地服务启动超时。请关闭旧的查价工具窗口和专用 Edge 后重试。");
    }

    private bool IsExpectedServer(string url, string expectedVersion)
    {
        try
        {
            using (var client = new WebClient())
            {
                client.Encoding = Encoding.UTF8;
                string json = client.DownloadString(url.TrimEnd('/') + "/api/config?t=" + DateTimeOffset.Now.ToUnixTimeMilliseconds());
                return json.Contains("\"app_version\":\"" + expectedVersion + "\"") ||
                    json.Contains("\"app_version\": \"" + expectedVersion + "\"");
            }
        }
        catch
        {
            return false;
        }
    }

    [STAThread]
    public static void Main()
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new LauncherForm());
    }
}
'@

Set-Content -LiteralPath $SourcePath -Value $Source -Encoding UTF8

& $Csc `
  /nologo `
  /target:winexe `
  /codepage:65001 `
  /out:$OutputExe `
  /reference:System.dll `
  /reference:System.Core.dll `
  /reference:System.Drawing.dll `
  /reference:System.Windows.Forms.dll `
  /reference:System.IO.Compression.dll `
  /reference:System.IO.Compression.FileSystem.dll `
  $SourcePath

Write-Host "Built launcher: $OutputExe"
