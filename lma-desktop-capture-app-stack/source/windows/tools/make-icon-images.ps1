<#
.SYNOPSIS
  Regenerate the tray-icon images used in the docs and the LMA web UI.

.DESCRIPTION
  Draws the idle (gray) and recording (red) tray icons with the SAME geometry and
  colors as IconFactory.Make() in App/TrayApp.cs, then writes a single side-by-side
  PNG showing both states with labels. Run this if the tray icon design changes so
  the documentation screenshot stays truthful.

  Output: ../../../../images/readme-audio-capture-windows-tray-icons.png

.EXAMPLE
  ./tools/make-icon-images.ps1
#>
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing

# Mirrors IconFactory.Make() in App/TrayApp.cs - keep in sync.
function New-TrayGlyph {
    param([bool]$Recording, [int]$Size = 128)

    $bmp = New-Object System.Drawing.Bitmap($Size, $Size)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.Clear([System.Drawing.Color]::Transparent)

    if ($Recording) { $bg = [System.Drawing.Color]::FromArgb(0xD4, 0x2A, 0x2A) }
    else            { $bg = [System.Drawing.Color]::FromArgb(0x53, 0x5B, 0x66) }
    $bgBrush = New-Object System.Drawing.SolidBrush($bg)
    $g.FillEllipse($bgBrush, 1, 1, $Size - 2, $Size - 2)

    $fg = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::White)
    # The C# code draws at 32px; scale those coordinates up for a crisp doc image.
    $k = $Size / 32.0
    if ($Recording) {
        $g.FillEllipse($fg, ($Size / 2 - 6 * $k), ($Size / 2 - 6 * $k), (12 * $k), (12 * $k))
    } else {
        $heights = @(6, 12, 9, 14, 7)
        $x = 7.0 * $k
        foreach ($h in $heights) {
            $g.FillRectangle($fg, $x, (($Size - $h * $k) / 2.0), (3 * $k), ($h * $k))
            $x += 4.5 * $k
        }
    }

    $fg.Dispose(); $bgBrush.Dispose(); $g.Dispose()
    return $bmp
}

$glyph = 96
$padX = 28
$labelH = 34
$gap = 150
$w = $padX * 2 + $glyph * 2 + $gap
$h = $glyph + $labelH + 24

$canvas = New-Object System.Drawing.Bitmap($w, $h)
$g = [System.Drawing.Graphics]::FromImage($canvas)
$g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
$g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::ClearTypeGridFit
$g.Clear([System.Drawing.Color]::White)

$font = New-Object System.Drawing.Font("Segoe UI", 11, [System.Drawing.FontStyle]::Regular)
$bold = New-Object System.Drawing.Font("Segoe UI", 11, [System.Drawing.FontStyle]::Bold)
$ink = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(0x1A, 0x1A, 0x1A))
$dim = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(0x5A, 0x5A, 0x5A))

$states = @(
    @{ Recording = $false; Title = "Idle"; Sub = "not recording" },
    @{ Recording = $true;  Title = "Recording"; Sub = "streaming to LMA" }
)

$x = $padX
foreach ($s in $states) {
    $bmp = New-TrayGlyph -Recording $s.Recording -Size 256
    $g.DrawImage($bmp, $x, 8, $glyph, $glyph)
    $bmp.Dispose()

    $t = $s.Title
    $tw = $g.MeasureString($t, $bold).Width
    $g.DrawString($t, $bold, $ink, ($x + $glyph / 2 - $tw / 2), ($glyph + 12))

    $sub = $s.Sub
    $sw = $g.MeasureString($sub, $font).Width
    $g.DrawString($sub, $font, $dim, ($x + $glyph / 2 - $sw / 2), ($glyph + 32))

    $x += $glyph + $gap
}

$g.Dispose()
$out = Join-Path $PSScriptRoot "..\..\..\..\images\readme-audio-capture-windows-tray-icons.png"
$out = [IO.Path]::GetFullPath($out)
$canvas.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
$canvas.Dispose()
$font.Dispose(); $bold.Dispose(); $ink.Dispose(); $dim.Dispose()
Write-Host "Wrote $out"
