/*
 * GalaxyCommunication dummy service -- wine-hardened.
 *
 * Why this exists:
 *   The Galaxy SDK refuses to sign in unless the Windows service
 *   "GalaxyCommunication" reports SERVICE_RUNNING to the SCM. The original
 *   Heroic dummy (communication.c) reports SERVICE_START_PENDING and then
 *   enters a worker loop WITHOUT ever calling SetServiceStatus(RUNNING).
 *   Real Windows / older wine tolerate this; wine 10 (flatpak org.winehq.Wine
 *   on Steam Deck) is stricter, so `sc start` times out (rc=2 / 1053) and the
 *   SDK sees the service as not running -> "start the GOG Galaxy client".
 *
 * Fix: do the full, prompt SCM startup handshake wine expects --
 *   START_PENDING (with checkpoint + short wait hint) -> RUNNING immediately --
 *   then idle until STOP. No network, no work; the SDK only checks status.
 *
 * Build (64-bit, matches the x64 game/prefix):
 *   x86_64-w64-mingw32-gcc communication_wine.c -o GalaxyCommunication.exe \
 *       -ladvapi32 -mwindows
 */

#include <windows.h>

#define SERVICE_NAME "GalaxyCommunication"

static SERVICE_STATUS        g_ServiceStatus = {0};
static SERVICE_STATUS_HANDLE g_StatusHandle  = NULL;
static HANDLE                g_StopEvent      = INVALID_HANDLE_VALUE;

static VOID WINAPI ServiceMain(DWORD argc, LPTSTR *argv);
static VOID WINAPI ServiceCtrlHandler(DWORD ctrl);

static void ReportStatus(DWORD state, DWORD checkpoint, DWORD waitHint)
{
    g_ServiceStatus.dwServiceType             = SERVICE_WIN32_OWN_PROCESS;
    g_ServiceStatus.dwCurrentState            = state;
    g_ServiceStatus.dwWin32ExitCode           = NO_ERROR;
    g_ServiceStatus.dwServiceSpecificExitCode = 0;
    g_ServiceStatus.dwCheckPoint              = checkpoint;
    g_ServiceStatus.dwWaitHint                = waitHint;
    if (state == SERVICE_START_PENDING)
        g_ServiceStatus.dwControlsAccepted = 0;
    else
        g_ServiceStatus.dwControlsAccepted = SERVICE_ACCEPT_STOP
                                           | SERVICE_ACCEPT_SHUTDOWN;
    SetServiceStatus(g_StatusHandle, &g_ServiceStatus);
}

int main(void)
{
    SERVICE_TABLE_ENTRY table[] = {
        { SERVICE_NAME, (LPSERVICE_MAIN_FUNCTION)ServiceMain },
        { NULL, NULL }
    };
    if (!StartServiceCtrlDispatcher(table))
        return (int)GetLastError();
    return 0;
}

static VOID WINAPI ServiceMain(DWORD argc, LPTSTR *argv)
{
    (void)argc; (void)argv;

    g_StatusHandle = RegisterServiceCtrlHandler(SERVICE_NAME,
                                                ServiceCtrlHandler);
    if (!g_StatusHandle)
        return;

    /* Tell the SCM we're starting -- gives wine's services.exe a definite
     * transition to observe instead of an abrupt jump to RUNNING. */
    ReportStatus(SERVICE_START_PENDING, 1, 2000);

    g_StopEvent = CreateEvent(NULL, TRUE, FALSE, NULL);
    if (!g_StopEvent) {
        ReportStatus(SERVICE_STOPPED, 0, 0);
        return;
    }

    /* Up and running -- report immediately so `sc start` returns 0. */
    ReportStatus(SERVICE_RUNNING, 0, 0);

    /* Idle until asked to stop. The service does nothing else; the SDK only
     * queries its status, the actual comms go to the launcher's commservice
     * listening on 127.0.0.1:9977. */
    WaitForSingleObject(g_StopEvent, INFINITE);

    ReportStatus(SERVICE_STOPPED, 0, 0);
}

static VOID WINAPI ServiceCtrlHandler(DWORD ctrl)
{
    switch (ctrl) {
        case SERVICE_CONTROL_STOP:
        case SERVICE_CONTROL_SHUTDOWN:
            ReportStatus(SERVICE_STOP_PENDING, 1, 2000);
            if (g_StopEvent != INVALID_HANDLE_VALUE)
                SetEvent(g_StopEvent);
            break;
        case SERVICE_CONTROL_INTERROGATE:
            /* Re-assert current status -- some SCM implementations poll this. */
            SetServiceStatus(g_StatusHandle, &g_ServiceStatus);
            break;
        default:
            break;
    }
}
