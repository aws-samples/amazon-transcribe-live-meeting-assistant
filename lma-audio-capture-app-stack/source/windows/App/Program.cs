using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;

namespace LMA;

/// <summary>
/// Entry point. Dual-mode dispatch (mirrors macOS main.swift):
///   • --selftest    → run SRP known-answer tests offline and exit.
///   • --login-only  → SRP login, print token metadata (not the token), exit.
///   • no --flags    → GUI system-tray app (double-clicked .exe).
///   • any --flag    → headless CLI streaming to stdout with the VU meter line.
///   • --cli forces CLI, --gui forces GUI.
///
/// Because OutputType=WinExe (no console window for the tray app), the CLI path
/// attaches to the parent console so its logs appear when launched from a terminal.
/// </summary>
public static class Program
{
    [DllImport("kernel32.dll")] private static extern bool AttachConsole(int dwProcessId);
    [DllImport("kernel32.dll")] private static extern bool AllocConsole();
    [DllImport("kernel32.dll")] private static extern IntPtr GetStdHandle(int nStdHandle);
    private const int ATTACH_PARENT_PROCESS = -1;
    private const int STD_OUTPUT_HANDLE = -11;

    [STAThread]
    public static int Main(string[] args)
    {
        // `--selftest`: validate the Cognito SRP signature against baked-in
        // known-answers (offline), then exit. Run this after any crypto change.
        if (args.Contains("--selftest"))
        {
            EnsureConsole();
            return SelfTest.Run();
        }

        // `--capture-test <seconds> <out.wav>`: offline audio verification — runs
        // the REAL WASAPI capture + StereoMixer + WavTee with NO socket, so you can
        // confirm ch0/Left=system, ch1/Right=mic (aligned, not swapped) by measuring
        // per-channel RMS on the resulting WAV. No token/server needed.
        if (args.Contains("--capture-test"))
        {
            EnsureConsole();
            return RunCaptureTest(args);
        }

        // Mode dispatch: with NO --flags (e.g. double-clicked .exe), launch the
        // tray GUI. With any --flag, run the headless CLI. --cli forces CLI even
        // with no other flags; --gui forces GUI.
        var userFlags = args.Where(a => a.StartsWith("--") && a != "--cli" && a != "--gui").ToArray();
        bool wantGui = args.Contains("--gui") || (userFlags.Length == 0 && !args.Contains("--cli"));

        if (wantGui)
        {
            // Tray app: no console. (If started from a terminal, don't spam it.)
            return TrayApp.Run(Config.Parse(args));
        }

        EnsureConsole();
        return RunCli(args);
    }

    /// <summary>
    /// Attach to the parent console (or allocate one) so CLI logs are visible,
    /// then wire Console's std handles to it. Must run before any Console use —
    /// a WinExe starts with no console, so touching Console first would throw.
    /// </summary>
    private static void EnsureConsole()
    {
        // If stdout is already bound to a file/pipe (redirected launch), leave it
        // alone — attaching a console would rebind the handles away from the
        // redirect and swallow the output. Only attach/alloc when we have no
        // usable stdout handle (double-clicked, or launched from a bare shell).
        var stdout = GetStdHandle(STD_OUTPUT_HANDLE);
        bool hasStdout = stdout != IntPtr.Zero && stdout != new IntPtr(-1);
        if (!hasStdout)
        {
            if (!AttachConsole(ATTACH_PARENT_PROCESS)) AllocConsole();
        }
        try { Console.OutputEncoding = Encoding.UTF8; } catch { /* redirected to a pipe */ }
    }

    // MARK: - Offline capture verification (no socket, no token)

    private static int RunCaptureTest(string[] args)
    {
        int seconds = 6;
        string outPath = "capture-test.wav";
        for (int i = 0; i < args.Length; i++)
        {
            if (args[i] == "--capture-test")
            {
                if (i + 1 < args.Length && int.TryParse(args[i + 1], out var s)) seconds = s;
                if (i + 2 < args.Length && !args[i + 2].StartsWith("--")) outPath = args[i + 2];
            }
        }

        int rate = 48000;
        Console.WriteLine($"Capture test: {seconds}s → {outPath} (ch0=Left=system, ch1=Right=mic)");
        Console.WriteLine("Play some system audio and speak into the mic now…\n");

        var tee = WavTee.Create(outPath, rate, 2);
        var mixer = new StereoMixer(rate, _ => { }); // no socket; the tee captures the exact PCM
        mixer.Tee = tee;
        mixer.SetConnected(true);
        var capture = new AudioCapture(mixer, rate);
        mixer.Start();
        capture.Start();
        Thread.Sleep(seconds * 1000);
        capture.Stop();
        mixer.Stop();
        tee?.Finish();
        Console.WriteLine($"\nDone. Wrote {outPath}. Measure per-channel RMS to confirm channel mapping.");
        return 0;
    }

    // MARK: - Headless CLI (parity with macOS main.swift CLI path)

    private static int RunCli(string[] args)
    {
        var config = Config.Parse(args);

        var err = config.Validate();
        // Login-only can proceed with just credentials; full run needs the endpoint too.
        bool loginOnly = args.Contains("--login-only");

        if (err != null && !(loginOnly && config.WantsLogin))
        {
            Console.Error.WriteLine($"Config error: {err}");
            return 2;
        }

        // In-app login: exchange username/password for Cognito tokens via SRP.
        if (config.WantsLogin)
        {
            var password = config.Password;
            if (string.IsNullOrEmpty(password))
            {
                password = PromptPassword($"Password for {config.Username}: ");
            }
            if (string.IsNullOrEmpty(password))
            {
                Console.Error.WriteLine($"No password provided for --username {config.Username}.");
                return 2;
            }
            Console.WriteLine($"Signing in as {config.Username} via Cognito SRP…");
            try
            {
                var tokens = Srp.LoginAsync(config.Username, password,
                    config.UserPoolId, config.ClientId, config.EffectiveRegion).GetAwaiter().GetResult();
                config.AccessToken = tokens.AccessToken;
                config.IdToken = tokens.IdToken;
                Console.WriteLine($"✓ Signed in — got Cognito tokens (access/id{(string.IsNullOrEmpty(tokens.RefreshToken) ? "" : "/refresh")})");
                if (loginOnly)
                {
                    PrintTokenMetadata(tokens.AccessToken);
                    return 0;
                }
            }
            catch (Exception e)
            {
                Console.Error.WriteLine($"Login failed: {e.Message}");
                return 1;
            }
        }
        else if (loginOnly)
        {
            Console.Error.WriteLine("--login-only requires --username/--password (or set LMA_USERNAME/LMA_PASSWORD).");
            return 2;
        }

        Console.WriteLine("LMA native audio client (Windows)");
        Console.WriteLine($"  endpoint : {config.Endpoint}");
        Console.WriteLine($"  callId   : {config.CallId}");
        Console.WriteLine($"  rate     : {config.SampleRate} Hz, 2ch interleaved 16-bit PCM");
        Console.WriteLine("  channels : ch0=meeting(system audio)  ch1=mic");
        Console.WriteLine();

        var socket = new TranscriberSocket(config);
        var mixer = new StereoMixer(config.SampleRate, chunk => socket.SendPcm(chunk));
        var capture = new AudioCapture(mixer, config.SampleRate);

        // Reflect live WS connection state in the meter so "sent" isn't misleading
        // while the socket is down (during reconnect audio is buffered, not sent).
        socket.OnStateChange = connected => mixer.SetConnected(connected);

        var done = new ManualResetEventSlim(false);
        socket.OnFatalAuth = _ => done.Set();

        // Optional: tee the exact streamed PCM to a local stereo WAV for offline
        // verification (per-channel RMS proves ch0=system / ch1=mic, not swapped).
        WavTee? debugTee = null;
        if (!string.IsNullOrEmpty(config.DebugWavPath))
        {
            debugTee = WavTee.Create(config.DebugWavPath, config.SampleRate, 2);
            mixer.Tee = debugTee;
            if (debugTee != null) Console.WriteLine($"  debug-wav: {config.DebugWavPath} (ch0=Left=system, ch1=Right=mic)");
        }

        bool stopped = false;
        void Shutdown()
        {
            if (stopped) return;
            stopped = true;
            Console.WriteLine("\nStopping…");
            capture.Stop();
            mixer.Stop();
            debugTee?.Finish();
            socket.BeginClose();   // mark intentional first so teardown stays quiet
            socket.SendEnd();
            Thread.Sleep(500);
            socket.Close();
            done.Set();
        }

        // Clean shutdown on Ctrl-C: send END, close socket, stop capture.
        Console.CancelKeyPress += (_, e) => { e.Cancel = true; Shutdown(); };

        // Interactive controls when attached to a TTY: 'm' toggles mic mute, 'q' quits.
        bool micMuted = false;
        bool interactive = !Console.IsInputRedirected;
        if (interactive)
        {
            Console.WriteLine("  controls : press 'm' to toggle mic mute, 'q' (or Ctrl-C) to stop");
            var keyThread = new Thread(() =>
            {
                try
                {
                    while (!done.IsSet)
                    {
                        var key = Console.ReadKey(intercept: true).KeyChar;
                        if (key is 'm' or 'M') { micMuted = !micMuted; mixer.SetMicMuted(micMuted); }
                        else if (key is 'q' or 'Q') { Shutdown(); break; }
                    }
                }
                catch { /* no console input available */ }
            }) { IsBackground = true };
            keyThread.Start();
        }

        socket.Connect();      // opens WS; sends START on open
        mixer.Start();         // begins 100ms flush cadence

        try
        {
            capture.Start();
            Console.WriteLine("\nStreaming… press Ctrl-C to stop.\n");
        }
        catch (Exception e)
        {
            Console.Error.WriteLine($"Capture failed to start: {e.Message}");
            Console.Error.WriteLine("If the mic failed, enable microphone access in Settings ▸ Privacy & security ▸ Microphone, then re-run.");
            return 1;
        }

        done.Wait();
        Shutdown();
        return 0;
    }

    private static string PromptPassword(string prompt)
    {
        Console.Write(prompt);
        var sb = new StringBuilder();
        try
        {
            while (true)
            {
                var key = Console.ReadKey(intercept: true);
                if (key.Key == ConsoleKey.Enter) { Console.WriteLine(); break; }
                if (key.Key == ConsoleKey.Backspace) { if (sb.Length > 0) sb.Length--; }
                else if (!char.IsControl(key.KeyChar)) sb.Append(key.KeyChar);
            }
        }
        catch { return Console.ReadLine() ?? ""; }
        return sb.ToString();
    }

    /// <summary>Print token metadata (not the token) for --login-only diagnostics.</summary>
    private static void PrintTokenMetadata(string accessToken)
    {
        var parts = accessToken.Split('.');
        if (parts.Length == 3)
        {
            var payload = Base64UrlDecode(parts[1]);
            if (payload != null)
            {
                try
                {
                    using var doc = JsonDocument.Parse(payload);
                    var root = doc.RootElement;
                    var user = root.TryGetProperty("username", out var u) ? u.ToString()
                             : root.TryGetProperty("sub", out var s) ? s.ToString() : "?";
                    Console.WriteLine($"  token user: {user}");
                    if (root.TryGetProperty("exp", out var expEl) && expEl.TryGetInt64(out var exp))
                        Console.WriteLine($"  expires:    {DateTimeOffset.FromUnixTimeSeconds(exp).UtcDateTime:u}");
                }
                catch { /* metadata is best-effort */ }
            }
        }
        Console.WriteLine($"  access token length: {accessToken.Length} chars");
    }

    private static byte[]? Base64UrlDecode(string s)
    {
        var b = s.Replace('-', '+').Replace('_', '/');
        while (b.Length % 4 != 0) b += "=";
        try { return Convert.FromBase64String(b); } catch { return null; }
    }
}
