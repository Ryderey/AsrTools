using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;

[assembly: AssemblyTitle("ASRTools")]
[assembly: AssemblyDescription("ASRTools Speech Recognition Tool")]
[assembly: AssemblyCompany("ASRTools")]
[assembly: AssemblyProduct("ASRTools")]
[assembly: AssemblyCopyright("Copyright (c) ASRTools contributors")]
[assembly: AssemblyVersion("__WINDOWS_FILE_VERSION__")]
[assembly: AssemblyFileVersion("__WINDOWS_FILE_VERSION__")]
[assembly: AssemblyInformationalVersion("__APP_VERSION__")]
[assembly: ComVisible(false)]

internal static class Program
{
    private const uint MbOk = 0x00000000;
    private const uint MbIconError = 0x00000010;

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int MessageBox(IntPtr window, string message, string caption, uint type);

    [STAThread]
    private static int Main(string[] args)
    {
        string packageRoot = AppDomain.CurrentDomain.BaseDirectory;
        string runtimeDirectory = Path.Combine(packageRoot, "_runtime");
        string runtimeExecutable = Path.Combine(runtimeDirectory, "ASRTools-runtime.exe");

        if (!File.Exists(runtimeExecutable))
        {
            ShowError(
                "未找到 ASRTools 运行文件。请完整解压便携包后再运行，" +
                "不要单独复制根目录中的 ASRTools.exe。"
            );
            return 2;
        }

        try
        {
            ProcessStartInfo startInfo = new ProcessStartInfo();
            startInfo.FileName = runtimeExecutable;
            startInfo.Arguments = BuildArgumentString(args);
            startInfo.WorkingDirectory = runtimeDirectory;
            startInfo.UseShellExecute = false;

            using (Process process = Process.Start(startInfo))
            {
                if (process == null)
                {
                    ShowError("ASRTools 启动失败，请重新获取完整便携包。");
                    return 3;
                }

                process.WaitForExit();
                return process.ExitCode;
            }
        }
        catch (Exception exception)
        {
            ShowError(
                "ASRTools 启动失败。请确认安全软件没有隔离 _runtime 目录中的文件。\n\n" +
                exception.Message
            );
            return 3;
        }
    }

    private static string BuildArgumentString(string[] args)
    {
        StringBuilder commandLine = new StringBuilder();
        for (int index = 0; index < args.Length; index++)
        {
            if (index > 0)
            {
                commandLine.Append(' ');
            }
            commandLine.Append(QuoteArgument(args[index]));
        }
        return commandLine.ToString();
    }

    private static string QuoteArgument(string argument)
    {
        if (argument.Length == 0)
        {
            return "\"\"";
        }

        bool needsQuotes = false;
        foreach (char character in argument)
        {
            if (char.IsWhiteSpace(character) || character == '"')
            {
                needsQuotes = true;
                break;
            }
        }
        if (!needsQuotes)
        {
            return argument;
        }

        StringBuilder quoted = new StringBuilder();
        quoted.Append('"');
        int backslashes = 0;
        foreach (char character in argument)
        {
            if (character == '\\')
            {
                backslashes++;
                continue;
            }

            if (character == '"')
            {
                quoted.Append('\\', backslashes * 2 + 1);
                quoted.Append('"');
                backslashes = 0;
                continue;
            }

            quoted.Append('\\', backslashes);
            quoted.Append(character);
            backslashes = 0;
        }

        quoted.Append('\\', backslashes * 2);
        quoted.Append('"');
        return quoted.ToString();
    }

    private static void ShowError(string message)
    {
        MessageBox(IntPtr.Zero, message, "ASRTools", MbOk | MbIconError);
    }
}
