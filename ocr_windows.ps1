# Windows built-in OCR (offline, CPU, no install) on fax PDFs. Shows what "free OCR" can read.
# Usage: powershell -File ocr_windows.ps1 faxes_in\01A182d7.PDF [more.pdf ...]
# Result on our test faxes: typed faxes come out perfect; handwritten fields come out empty or garbled.
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Pdfs)

$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $t) { $task = $asTask.MakeGenericMethod($t).Invoke($null, @($op)); $task.Wait(-1) | Out-Null; $task.Result }

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
$tmp = Join-Path $env:TEMP "fax_ocr"; New-Item -ItemType Directory -Force $tmp | Out-Null

foreach ($pdf in $Pdfs) {
    $stem = [IO.Path]::GetFileNameWithoutExtension($pdf)
    # every page (up to 4) as its own PNG: page_stem_0.png, page_stem_1.png, ...
    python -c "import pypdfium2 as p, sys; d=p.PdfDocument(sys.argv[1]); [d[i].render(scale=2.0).to_pil().convert('RGB').save(sys.argv[2] + '_%d.png' % i) for i in range(min(len(d), 4))]" $pdf (Join-Path $tmp $stem)
    "===== $pdf ====="
    foreach ($png in (Get-ChildItem (Join-Path $tmp ($stem + "_*.png")) | Sort-Object Name)) {
        $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($png.FullName)) ([Windows.Storage.StorageFile])
        $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bmp = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $res = Await ($engine.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult])
        $res.Text
        $stream.Dispose()
        Remove-Item $png.FullName -Force
    }
}
