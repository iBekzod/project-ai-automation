# =============================================================================
# Xonsaroy AI PM Bot — avtomatik ishga tushirishni o'rnatish
# =============================================================================
#
# Kompyuter o'chirib yoqilganda ham bot o'zi ishga tushsin.
#
# NEGA XIZMAT (service) EMAS, VAZIFA (scheduled task):
#   Bot Claude CLI ni chaqiradi, uning autentifikatsiyasi esa foydalanuvchi
#   profilida (%USERPROFILE%\.claude) turadi. SYSTEM nomidan ishlaydigan xizmat
#   u profilni ko'rmaydi va Claude birinchi chaqiruvdayoq yiqiladi.
#   Shuning uchun vazifa AYNAN shu foydalanuvchi nomidan, u tizimga kirganda
#   ishga tushadi.
#
# CHEKLOV: kompyuter yoqilib, lekin hech kim tizimga kirmasa — bot ishlamaydi.
#   Buni bilib turish kerak. Doimiy ishlashi shart bo'lsa, botni serverga
#   ko'chirish kerak (u yerda `agent-runner` allaqachon bor).
#
# pythonw.exe ishlatiladi — konsol oynasi ochilmaydi. Loglar repo papkasidagi
# fayllarga yoziladi (main.py o'zi sozlaydi).
#
# Ishlatish:
#   powershell -ExecutionPolicy Bypass -File autostart_install.ps1
#   powershell -ExecutionPolicy Bypass -File autostart_install.ps1 -Remove
# =============================================================================

param([switch]$Remove)

$TaskName = "XonsaroyAiPmBot"
$Root     = Split-Path -Parent $MyInvocation.MyCommand.Path
$Pythonw  = Join-Path $Root ".venv\Scripts\pythonw.exe"
$Script   = Join-Path $Root "main.py"

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        "o'chirildi: $TaskName"
    } else {
        "topilmadi: $TaskName"
    }
    return
}

foreach ($p in @($Pythonw, $Script)) {
    if (-not (Test-Path $p)) { throw "topilmadi: $p" }
}

$action = New-ScheduledTaskAction -Execute $Pythonw -Argument "`"$Script`"" -WorkingDirectory $Root

# Tizimga kirganda ishga tushadi. 30 soniya kechikish — tarmoq va disk
# tayyor bo'lishi uchun; ularsiz bot birinchi so'rovda yiqilishi mumkin.
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = "PT30S"

# Yiqilsa qayta ko'tariladi. Muddat cheklanmaydi — bot uzoq ishlaydigan
# jarayon, uni "3 kundan keyin to'xtat" degan sukut sozlama o'ldirardi.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartInterval (New-TimeSpan -Minutes 2) `
    -RestartCount 3 `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force `
    -Description "Xonsaroy AI PM Telegram bot. Tizimga kirganda avtomat ishga tushadi." | Out-Null

"o'rnatildi: $TaskName"
"  dastur : $Pythonw"
"  skript : $Script"
"  trigger: tizimga kirganda (+30s)"
"  qayta  : yiqilsa 2 daqiqada, 3 martagacha"
""
"Hozir ishga tushirish:   Start-ScheduledTask -TaskName $TaskName"
"To'xtatish:              Stop-ScheduledTask  -TaskName $TaskName"
"Holat:                   Get-ScheduledTask   -TaskName $TaskName"
