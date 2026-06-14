using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Net;
using DataAccessProvider;
using GameplayVisuals.Battle;
using GwentGameplay;
using GwentGameplay.AI;
using GwentGameplay.Settings;
using GwentUnity;
using GwentVisuals;
using GwentWebServiceClient.DTO;
using GwentWebServiceClient.DTO.Arguments;
using GwentWebServiceClient.Services;
using HarmonyLib;
using Localization;
using MelonLoader;
using Newtonsoft.Json;
using RedLogger;
using RedTools;
using RedNetwork;
using RedNetwork.Authenticator.Offline;
using RedTools.Code.Exceptions;
using GwentUnity.Social;
using SocialFeatures.Manager;
using SocialFeatures.DataSource;
using SocialFeatures.Model;

// DataAccessProvider contains CollectionManager / ACollectionController / CollectionDeck

[assembly: MelonInfo(typeof(GwentBetaRestorationMod.GwentBetaModMain), "GwentBetaRestorationMod", "1.0.0", "Somnia", null)]
[assembly: MelonGame("CDProjektRED", "Gwent")]

namespace GwentBetaRestorationMod
{
    public class GwentBetaModMain : MelonMod
    {
        private static HarmonyLib.Harmony _harmony;
        private static bool _latePatched = false;
        private static int _frameCount = 0;
        private static bool _warnedWaitingForSettings = false;

        public override void OnInitializeMelon()
        {
            LoggerInstance.Msg("Gwent Beta Restoration Mod loaded.");

            _harmony = new HarmonyLib.Harmony("com.gwentbeta.restoration");

            // --- SSL cert bypass (eliminates Fiddler) ---
            Patch_GOGCertificateValidator.Apply(_harmony);

            _harmony.PatchAll(typeof(GwentBetaModMain).Assembly);
            LoggerInstance.Msg("All Harmony patches applied (except deferred ones).");

            try
            {
                var patchedMethods = HarmonyLib.Harmony.GetAllPatchedMethods();
                foreach (var method in patchedMethods)
                {
                    var patchInfo = HarmonyLib.Harmony.GetPatchInfo(method);
                    if (patchInfo != null && patchInfo.Owners.Contains("com.gwentbeta.restoration"))
                    {
                        LoggerInstance.Msg("  Patched: " + method.DeclaringType.Name + "." + method.Name);
                    }
                }
            }
            catch (System.Exception ex)
            {
                LoggerInstance.Error("Failed to list patches: " + ex.Message);
            }
        }

        public override void OnLateUpdate()
        {
            if (!_latePatched)
            {
                _frameCount++;

                // The deferred BattleSetupFactory patch can ONLY be applied once
                // RedTools.GwentSettings has finished its (I/O-bound) init.
                // Harmony-patching BattleSetupFactory.CreateClientConnector forces
                // that type's ..cctor to run, and the ..cctor reads
                // RedKit.Settings -> GwentSettings.Instance. If GwentSettings isn't
                // ready yet, get_Instance throws, which surfaces as a
                // TypeInitializationException and PERMANENTLY poisons
                // BattleSetupFactory for the rest of the process (no retry possible).
                //
                // A fixed frame count was a proxy for "settings are ready" and is
                // inherently racy: on a slow-initializing / fast-rendering host the
                // frame counter outran asset init and the patch fired too early,
                // poisoning the type (this is exactly the IL Compile Error /
                // "GwentSettings are not yet initialized" crash). Instead, probe the
                // real dependency: try to touch GwentSettings.Instance directly (the
                // same call the ..cctor makes) inside a try/catch. While it is not
                // ready the getter throws, we swallow it and try again next frame;
                // touching GwentSettings.Instance does NOT touch BattleSetupFactory,
                // so nothing gets poisoned. Only once the probe succeeds do we let
                // Harmony patch BattleSetupFactory, at which point its ..cctor is
                // guaranteed to succeed.
                //
                // A small frame floor avoids hammering the getter from the very
                // first frames; readiness, not the floor, is what actually gates.
                if (_frameCount >= 60 && IsGwentSettingsReady())
                {
                    _latePatched = true;
                    try
                    {
                        Patch_BattleSetupFactory_CreateClientConnector.ApplyManually(_harmony);
                        LoggerInstance.Msg("BattleSetupFactory patch applied successfully (settings ready at frame " + _frameCount + ").");

                        Patch_RequestPlayCardAction_ForceResolve.ApplyManually(_harmony);
                        Patch_TurnGameState_OnUpdate.ApplyManually(_harmony);
                        Patch_AGameState_HandleDirtyGameController.ApplyManually(_harmony);
                        LoggerInstance.Msg("Timeout end-turn patches applied successfully.");

                    }
                    catch (System.Exception ex)
                    {
                        LoggerInstance.Error("Failed to apply BattleSetupFactory patch: " + ex);
                    }
                }
                else if (!_warnedWaitingForSettings && _frameCount >= 60)
                {
                    _warnedWaitingForSettings = true;
                    LoggerInstance.Msg("Waiting for GwentSettings to initialize before applying BattleSetupFactory patch...");
                }
            }
        }

        // Returns true once RedTools.GwentSettings is initialized enough that
        // touching its Instance no longer throws. Probing Instance is exactly what
        // BattleSetupFactory's static ctor does internally, so a successful probe
        // here guarantees the ..cctor will not throw when Harmony patches the type.
        // IMPORTANT: this must NOT reference BattleSetupFactory in any way, or it
        // would trigger (and potentially poison) that type's initializer early.
        private static bool IsGwentSettingsReady()
        {
            try
            {
                return RedTools.GwentSettings.Instance != null;
            }
            catch
            {
                // Not initialized yet ("GwentSettings are not yet initialized...").
                return false;
            }
        }
    }

    // =========================================================================
    // PATCH: VirtualCurrencyPurchaseController.OnStartTransactionSuccess
    // =========================================================================
    [HarmonyPatch(typeof(VirtualCurrencyPurchaseController))]
    [HarmonyPatch("OnStartTransactionSuccess")]
    public static class Patch_VirtualCurrencyPurchaseController_OnStartTransactionSuccess
    {
        private static FieldInfo f_GwentWSFacade = AccessTools.Field(typeof(VirtualCurrencyPurchaseController), "m_GwentWSFacade");
        private static FieldInfo f_CurrentPurchaseArguments = AccessTools.Field(typeof(VirtualCurrencyPurchaseController), "m_CurrentPurchaseArguments");
        private static FieldInfo f_CurrentTransaction = AccessTools.Field(typeof(BaseShopPurchaseFlow), "m_CurrentTransaction");
        private static FieldInfo f_CurrentUserId = AccessTools.Field(typeof(BaseShopPurchaseFlow), "m_CurrentUserId");
        private static FieldInfo f_TransactionFailed = AccessTools.Field(typeof(BaseShopPurchaseFlow), "m_TransactionFailed");
        private static MethodInfo m_StartItemReceiveTimer = AccessTools.Method(typeof(BaseShopPurchaseFlow), "StartItemReceiveTimer");
        private static MethodInfo m_OnTransactionFail_ShopEx = AccessTools.Method(typeof(BaseShopPurchaseFlow), "OnTransactionFail", new Type[] { typeof(ShopException) });

        static bool Prefix(VirtualCurrencyPurchaseController __instance, ShopTransaction shopTransaction)
        {
            f_CurrentTransaction.SetValue(__instance, shopTransaction);

            var purchaseArgs = (UserPurchaseFlowArguments)f_CurrentPurchaseArguments.GetValue(__instance);
            purchaseArgs.TransactionId = new ulong?(shopTransaction.Transaction.Id);
            purchaseArgs.UserId = new ulong?((ulong)f_CurrentUserId.GetValue(__instance));

            m_StartItemReceiveTimer.Invoke(__instance, null);

            int currency = Singleton<InventoryManager>.Instance.GetCurrency(EVirtualCurrencyType.Gold);
            int expectedBalance = currency - shopTransaction.Transaction.TotalPrice;
            if (expectedBalance < 0)
            {
                ShopException ex = new ShopException(
                    "Currency balance not matching",
                    ShopPurchaseError.InternalError,
                    "invalid_currency_amount",
                    Localization.LocalizationManager.Instance.GetTranslationText("invalid_currency_amount"),
                    0);
                m_OnTransactionFail_ShopEx.Invoke(__instance, new object[] { ex });
                return false;
            }

            VirtualPaymentData paymentData = new VirtualPaymentData
            {
                ExpectedFinalCurrencyAmount = expectedBalance
            };

            var gwentWSFacade = (GwentWSFacade)f_GwentWSFacade.GetValue(__instance);
            gwentWSFacade.FinishTransaction(
                purchaseArgs,
                new Action(() => OnFinishTransactionSuccess(__instance)),
                new Action<RedTools.Code.Exceptions.RestException>(ex => OnTransactionFailWrapper(__instance, ex)),
                paymentData);

            return false;
        }

        private static void OnFinishTransactionSuccess(VirtualCurrencyPurchaseController instance)
        {
            var gwentWSFacade = (GwentWSFacade)f_GwentWSFacade.GetValue(instance);
            ulong userId = (ulong)f_CurrentUserId.GetValue(instance);
            var transaction = (ShopTransaction)f_CurrentTransaction.GetValue(instance);

            gwentWSFacade.ForceDeliverProducts(
                userId,
                transaction.Transaction.Id,
                new Action(() => OnDeliverySuccess(instance)),
                new Action<RedTools.Code.Exceptions.RestException>(ex => OnTransactionFailWrapper(instance, ex)));
        }

        private static void OnDeliverySuccess(VirtualCurrencyPurchaseController instance)
        {
            var f_NotifTimer = AccessTools.Field(typeof(BaseShopPurchaseFlow), "m_CurrentNotificationTimer");
            var timer = f_NotifTimer.GetValue(instance);
            if (timer != null)
            {
                TimerManager.StopTimer((System.Collections.IEnumerator)timer);
                f_NotifTimer.SetValue(instance, null);
            }

            Singleton<InventoryManager>.Instance.FetchUserCurrencies();

            var currentTransaction = (ShopTransaction)f_CurrentTransaction.GetValue(instance);
            var succeeded = (Action<ShopTransaction>)AccessTools.Field(typeof(BaseShopPurchaseFlow), "m_TransactionSucceded").GetValue(instance);

            List<UserItem> newItems = new List<UserItem>();
            long ticks = DateTime.UtcNow.Ticks;

            var availableProducts = (List<ShopProduct>)AccessTools.Field(typeof(ShopManager), "m_AvailableProducts")
                .GetValue(Singleton<ShopManager>.Instance);

            foreach (ShopTransaction.Product product in currentTransaction.Transaction.ProductsList)
            {
                ShopProduct shopProduct = null;
                for (int i = 0; i < availableProducts.Count; i++)
                {
                    if (availableProducts[i].Id == product.PlatformProduct.Id)
                    {
                        shopProduct = availableProducts[i];
                        break;
                    }
                }

                if (shopProduct != null && shopProduct.Items != null)
                {
                    for (int j = 0; j < shopProduct.Items.Count; j++)
                    {
                        ShopProductDefinition.ItemData itemData = shopProduct.Items[j];
                        if (itemData.Type == ShopProductDefinition.ItemData.ItemDataType.inventory_item)
                        {
                            ItemDefinition itemDefinition = null;

                            var itemDefinitions = (List<ItemDefinition>)AccessTools.Field(typeof(InventoryManager), "m_ItemDefinitions")
                                .GetValue(Singleton<InventoryManager>.Instance);

                            for (int k = 0; k < itemDefinitions.Count; k++)
                            {
                                if (itemDefinitions[k].Id == itemData.Id)
                                {
                                    itemDefinition = itemDefinitions[k];
                                    break;
                                }
                            }
                            if (itemDefinition != null)
                            {
                                int count = itemData.Count * product.Count;
                                for (int l = 0; l < count; l++)
                                {
                                    newItems.Add(new UserItem
                                    {
                                        Id = (ulong)(ticks + (long)newItems.Count),
                                        State = UserItem.ItemState.New,
                                        ItemDefinition = itemDefinition
                                    });
                                }
                            }
                        }
                    }
                }
            }

            AccessTools.Method(typeof(InventoryManager), "AddItems")
                .Invoke(Singleton<InventoryManager>.Instance, new object[] { newItems });

            var clearMethod = AccessTools.Method(typeof(VirtualCurrencyPurchaseController), "ClearCurrentData");
            clearMethod.Invoke(instance, null);
            if (succeeded != null)
            {
                succeeded(currentTransaction);
            }
        }

        private static void OnTransactionFailWrapper(VirtualCurrencyPurchaseController instance, Exception ex)
        {
            var failMethod = AccessTools.Method(typeof(BaseShopPurchaseFlow), "OnTransactionFail", new Type[] { typeof(Exception) });
            failMethod.Invoke(instance, new object[] { ex });
        }
    }

    // =========================================================================
    // PATCH: AppMatchmakingGOGState.OnStartMatchmaking — capture chosen deck
    //
    // Problem 1 fix: the GOG matchmaking path never writes Params["Deck"] (unlike
    // the Sockets path which does so explicitly). By the time LobbyInitialized
    // fires the relay has already written the same stale deck to %TEMP% that it
    // had at startup.
    //
    // This Postfix runs immediately after OnStartMatchmaking, at the moment the
    // player commits to queuing with their selected deck. It:
    //   1. Reads the active CollectionDeck from CollectionManager.
    //   2. Converts it to a BattleDeck via ToBattleDeck().
    //   3. Serialises it to JSON and caches it in DeckCache.LocalDeckJson.
    //   4. Appends the deck to %TEMP%\gwent_relay_deck_queue.json (mutex-protected).
    //      Queue index 0 = first client to queue (C1/P1), index 1 = second (C2/P2).
    //      The relay reads queue[src_player_id-1] when injecting Deck into each
    //      PlayerInitialized, giving each client its own deck even on the same machine.
    //   5. Writes %TEMP%\gwent_relay_deck_0.json as a last-resort fallback.
    // =========================================================================

    /// <summary>
    /// Holds deck JSON strings captured at queue time and/or pushed by the relay,
    /// so the LobbyInitialized lambda always reads the freshest deck regardless of
    /// whether the %TEMP% files are stale or absent.
    ///
    /// Slot semantics:
    ///   LocalDeckJson — the deck this client queued with (set by OnStartMatchmaking patch).
    ///   P1DeckJson    — P1's BattleDeck JSON (set by relay deck-push executor, TypeID=0x42).
    ///   P2DeckJson    — P2's BattleDeck JSON (set by relay deck-push executor, TypeID=0x42).
    ///
    /// In cross-network and same-machine play, P1DeckJson ≠ P2DeckJson because the
    /// relay delivers each player's correct deck to both clients just before
    /// LobbyInitialized fires (using the queue-based injection in relay.py).
    /// </summary>
    public static class DeckCache
    {
        /// <summary>JSON string of the local player's BattleDeck, set at queue time. Null until queued.</summary>
        public static string LocalDeckJson = null;

        /// <summary>P1's BattleDeck JSON, pushed by the relay before LobbyInitialized. Null until received.</summary>
        public static string P1DeckJson = null;

        /// <summary>P2's BattleDeck JSON, pushed by the relay before LobbyInitialized. Null until received.</summary>
        public static string P2DeckJson = null;

        /// <summary>Resolve the best available JSON for P1's deck (relay push only).</summary>
        /// <remarks>
        /// We do NOT fall back to LocalDeckJson here: LocalDeckJson is always "my own deck",
        /// which is correct for P1 on C1 but wrong for P1 on C2 (C2's local deck is P2's deck).
        /// The relay push (TypeID=0x42) always delivers the correct per-slot deck to each client,
        /// so we rely exclusively on that. The file-based fallback in LobbyInitialized handles
        /// the case where the relay push hasn't arrived yet.
        /// </remarks>
        public static string ResolveP1(out string source)
        {
            if (P1DeckJson != null) { source = "relay-push P1"; return P1DeckJson; }
            source = null; return null;
        }

        /// <summary>Resolve the best available JSON for P2's deck (relay push only).</summary>
        public static string ResolveP2(out string source)
        {
            if (P2DeckJson != null) { source = "relay-push P2"; return P2DeckJson; }
            source = null; return null;
        }
    }

    // =========================================================================
    // PlayerInfoCache — mirrors DeckCache for vanity / profile data.
    //
    // Slot semantics:
    //   P1InfoJson — P1's PlayerInfo JSON, pushed by relay TypeID=0x43 before LobbyInitialized.
    //   P2InfoJson — P2's PlayerInfo JSON, pushed by relay TypeID=0x43 before LobbyInitialized.
    //
    // The JSON is the native GwentGameplay.PlayerInfo serialisation that the game
    // already puts into CurrentUser.Params["PlayerInfo"] in OnStartMatchmaking.
    // Shape: {"Name":"…","Title":"…","Personality":{"PersonalityName":"…"},
    //         "Level":1,"MMR":-1,"Rank":-1,
    //         "Vanity":{"AvatarId":40008,"TauntPackId":0,"BorderId":39999,"TitleId":29999,"BoardId":0}}
    // =========================================================================
    public static class PlayerInfoCache
    {
        public static string P1InfoJson = null;
        public static string P2InfoJson = null;
    }

    [HarmonyPatch(typeof(AppMatchmakingGOGState))]
    [HarmonyPatch("OnStartMatchmaking")]
    public static class Patch_AppMatchmakingGOGState_OnStartMatchmaking
    {
        // Guard: OnStartMatchmaking fires once per player slot (P1 and P2 both call it),
        // so it runs twice per process. We only want to capture the deck once — the first
        // call captures the deck the local player actually chose.
        // Reset between matches by setting this back to false before queuing again.
        public static bool Captured = false;

        static void Postfix()
        {
            CaptureAndInjectDeck("matchmaking");
        }

        // Shared by the matchmaking postfix and the friend-match (PWF) postfix below.
        internal static void CaptureAndInjectDeck(string source)
        {
            if (Captured) return;
            Captured = true;
            MelonLogger.Msg("[DeckCapture] capture triggered by " + source);
            try
            {
                ACollectionController controller =
                    ASingleton<CollectionManager>.Instance.CurrentController;
                if (controller == null)
                {
                    MelonLogger.Warning("[DeckCapture] CollectionManager.CurrentController is null — cannot capture deck");
                    return;
                }

                CollectionDeck activeDeck = controller.GetActiveDeck();
                if (activeDeck == null)
                {
                    MelonLogger.Warning("[DeckCapture] GetActiveDeck() returned null — cannot capture deck");
                    return;
                }

                BattleDeck battleDeck = activeDeck.ToBattleDeck();
                if (battleDeck == null)
                {
                    MelonLogger.Warning("[DeckCapture] ToBattleDeck() returned null — cannot capture deck");
                    return;
                }

                string deckJson = JsonConvert.SerializeObject(battleDeck);

                // Cache locally — relay push (TypeID=0x42) will set P1DeckJson/P2DeckJson
                // with the correct per-slot decks just before LobbyInitialized fires.
                DeckCache.LocalDeckJson = deckJson;
                DeckCache.P1DeckJson = null;
                DeckCache.P2DeckJson = null;

                // ── Cross-network fix: inject deck into CurrentUser.Params["Deck"] ──
                // The GOG matchmaking path (AppMatchmakingGOGState) never sets
                // Params["Deck"], unlike the Sockets path which does. Without this,
                // the deck only travels via %TEMP% files (inaccessible across machines).
                // By setting Params["Deck"] here, the deck is included in the
                // PlayerInitialized wire message and relay.py can read it directly.
                try
                {
                    // GlobalNetworkManager and LobbyManager are internal — use reflection
                    var gnmType = typeof(AppMatchmakingGOGState).Assembly.GetType("GwentUnity.GlobalNetworkManager");
                    var gnmInstance = gnmType?.GetMethod("get_Instance", BindingFlags.Static | BindingFlags.Public | BindingFlags.FlattenHierarchy)
                                         ?.Invoke(null, null)
                                     ?? gnmType?.GetProperty("Instance", BindingFlags.Static | BindingFlags.Public | BindingFlags.FlattenHierarchy)
                                         ?.GetValue(null, null);
                    if (gnmInstance == null)
                    {
                        // Try Singleton<T> pattern
                        var singletonType = typeof(Singleton<>).MakeGenericType(gnmType);
                        gnmInstance = singletonType.GetProperty("Instance", BindingFlags.Static | BindingFlags.Public)
                                         ?.GetValue(null, null);
                    }
                    if (gnmInstance != null)
                    {
                        var lobbyProp = gnmInstance.GetType().GetProperty("LobbyManager",
                            BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                        var lobbyMgr = lobbyProp?.GetValue(gnmInstance, null) as ILobbyManager;
                        if (lobbyMgr != null && lobbyMgr.CurrentUser != null)
                        {
                            lobbyMgr.CurrentUser.Params["Deck"] = deckJson;
                            MelonLogger.Msg("[DeckCapture] Injected deck into CurrentUser.Params[\"Deck\"] for wire transport");
                        }
                    }
                }
                catch (Exception exP)
                {
                    MelonLogger.Warning("[DeckCapture] Failed to inject Params[\"Deck\"]: " + exP.Message);
                }

                // Reset PlayerInfo cache — relay push (TypeID=0x43) will fill these
                // from the native PlayerInfo JSON already in PlayerInitialized params.
                PlayerInfoCache.P1InfoJson = null;
                PlayerInfoCache.P2InfoJson = null;
                MelonLogger.Msg("[PlayerInfoCapture] PlayerInfoCache reset; relay will push native PlayerInfo JSON");

                string tmp = Path.GetTempPath();

                // Always write the generic deck_0.json as a last-resort fallback.
                File.WriteAllText(Path.Combine(tmp, "gwent_relay_deck_0.json"), deckJson);

                // ── Queue-based per-slot deck file (same-machine fix) ─────────────────
                // On same-machine testing both clients share %TEMP%, so a single keyed
                // file (service-ID or otherwise) gets overwritten by the second client.
                // Solution: append to a shared JSON array in a mutex-protected file.
                //   Index 0 = first client to queue  → relay reads as C1/P1
                //   Index 1 = second client to queue → relay reads as C2/P2
                // The relay clears this file at session start, so stale entries from a
                // previous match are never used.
                string queuePath = Path.Combine(tmp, "gwent_relay_deck_queue.json");
                try
                {
                    using (var mtx = new System.Threading.Mutex(false, "Global\\GwentRelayDeckQueue"))
                    {
                        bool acquired = mtx.WaitOne(3000); // wait up to 3 s
                        try
                        {
                            List<string> queue = new List<string>();
                            if (acquired && File.Exists(queuePath))
                            {
                                try { queue = JsonConvert.DeserializeObject<List<string>>(File.ReadAllText(queuePath)) ?? new List<string>(); }
                                catch { queue = new List<string>(); }
                            }
                            // If 2 entries already exist it is a stale file from a previous
                            // session that the relay hasn't cleared yet — reset rather than
                            // appending a third entry.
                            if (queue.Count >= 2) queue.Clear();
                            queue.Add(deckJson);
                            File.WriteAllText(queuePath, JsonConvert.SerializeObject(queue));
                            MelonLogger.Msg(string.Format("[DeckCapture] Wrote to queue index {0} ({1} bytes)", queue.Count - 1, deckJson.Length));
                        }
                        finally { if (acquired) mtx.ReleaseMutex(); }
                    }
                }
                catch (Exception exQ)
                {
                    MelonLogger.Warning("[DeckCapture] Queue write failed (relay will use deck_0.json fallback): " + exQ.Message);
                }

                MelonLogger.Msg(string.Format("[DeckCapture] Captured deck: faction={0} leader={1} cards={2}",
                    battleDeck.FactionId,
                    battleDeck.Leader != null ? battleDeck.Leader.TemplateId.ToString() : "null",
                    battleDeck.Cards != null ? battleDeck.Cards.Count.ToString() : "0"));

            }
            catch (Exception ex)
            {
                MelonLogger.Error("[DeckCapture] OnStartMatchmaking Postfix failed: " + ex.Message);
            }
        }
    }

    // =========================================================================
    // PATCH 1: BattleSetupFactory.CreateClientConnector (4-arg overload)
    //
    // Full method replacement (Prefix returning false) matching the DNSpy-patched
    // version exactly. Key differences from original:
    //   1. Passes null for playerInitializator (was InitializeHumanPlayer delegate)
    //   2. Entire LobbyInitialized lambda replaced: loads decks from %TEMP% JSON,
    //      reflects GameMode->Server for P1, runs extensive diagnostics logging,
    //      sets up ORPHAN-FULFILL on OnPlayedCard, registers tick timer, and
    //      fires OnGameInstanceStarted callback.
    //   3. AI branch passes null for AIPersonality (was local variable aipersonality)
    // =========================================================================
    public static class Patch_BattleSetupFactory_CreateClientConnector
    {
        // Per-match TICK diagnostic timer; disposed/replaced each LobbyInitialized so old
        // matches stop firing (prevents the two-games-in-one-log artifact + a resource leak).
        private static System.Threading.Timer _tickTimer = null;

        public static void ApplyManually(HarmonyLib.Harmony harmony)
        {
            var original = AccessTools.Method(
                typeof(GwentVisuals.BattleSetupFactory),
                "CreateClientConnector",
                new Type[] { typeof(RedNetworkManager), typeof(ILobbyManager), typeof(Action<OnlineNetworkConnector>), typeof(bool) });
            var prefix = AccessTools.Method(typeof(Patch_BattleSetupFactory_CreateClientConnector), "Prefix");
            harmony.Patch(original, prefix: new HarmonyMethod(prefix));
        }

        static bool Prefix(
            ref OnlineNetworkConnector __result,
            RedNetworkManager manager,
            ILobbyManager lobby,
            Action<OnlineNetworkConnector> onLobbyInitialized,
            bool isHuman)
        {
            // Access private static members of BattleSetupFactory via reflection
            Random m_Random = (Random)typeof(BattleSetupFactory)
                .GetField("m_Random", BindingFlags.Static | BindingFlags.NonPublic)
                .GetValue(null);
            IRedLogger logger = (IRedLogger)typeof(BattleSetupFactory)
                .GetProperty("Logger", BindingFlags.Static | BindingFlags.NonPublic)
                .GetValue(null, null);
            GameSharedData sharedData = (GameSharedData)typeof(BattleSetupFactory)
                .GetProperty("SharedData", BindingFlags.Static | BindingFlags.NonPublic)
                .GetValue(null, null);

            ulong num = ((!(manager.Authenticator is OfflineAuthenticator))
                ? manager.Authenticator.ServiceId
                : Convert.ToUInt64(m_Random.Next(1, 100000)));

            OnlineNetworkConnector connector = new OnlineNetworkConnector(lobby, logger, num, null);
            GameInstance game = new GameInstance(logger, EGameMode.Client, sharedData, null, null);
            // Turn timers are ENABLED. The timeout-stall is now handled by
            // Patch_RequestPlayCardAction_ForceResolve + Patch_TurnGameState_OnUpdate
            // (NotifyPlayerHasNoOptions clears the play-request mirrors on both clients,
            // then the IsDirty-bypass ends the turn without the finished-handshake that
            // deadlocked). See TIMEOUT_FIX_DESIGN_v16.md.
            connector.Initialize(game);

            lobby.RegisterEvent(LobbyEventType.LobbyInitialized).AddListener(delegate(ILobbyManager netLobby, LobbyEventArgs args)
            {
                string text = "CLIENT LobbyInitialized\n";
                try
                {
                    string text2 = Path.Combine(Path.GetTempPath(), "gwent_relay_deck_0.json");
                    string text3 = Path.Combine(Path.GetTempPath(), "gwent_relay_deck_1.json");
                    text = string.Concat(new string[]
                    {
                        text,
                        "p0 exists=",
                        File.Exists(text2).ToString(),
                        " p1 exists=",
                        File.Exists(text3).ToString(),
                        " localCache=",
                        (DeckCache.LocalDeckJson != null).ToString(),
                        " relayP1=",
                        (DeckCache.P1DeckJson != null).ToString(),
                        " relayP2=",
                        (DeckCache.P2DeckJson != null).ToString(),
                        "\n"
                    });

                    // ── P1 deck (own deck) ────────────────────────────────────────────────
                    // Priority: relay push (TypeID=0x42) > local capture (OnStartMatchmaking)
                    //           > %TEMP% file fallback (relay wrote it at SetupPlayers time).
                    // The relay push and local capture are both "fresh for this match";
                    // the file is a best-effort fallback for single-machine testing when
                    // neither cache is populated yet.
                    string p1Source;
                    string p1Json = DeckCache.ResolveP1(out p1Source);
                    if (p1Json == null && File.Exists(text2))
                    {
                        p1Json = File.ReadAllText(text2);
                        p1Source = "file";
                    }
                    if (p1Json != null)
                    {
                        game.Settings.P1.Deck = JsonConvert.DeserializeObject<BattleDeck>(p1Json);
                        text = text + "P1 deck faction=" + game.Settings.P1.Deck.FactionId.ToString()
                               + " src=" + p1Source + "\n";
                    }

                    // ── P2 deck (opponent deck) ───────────────────────────────────────────
                    // Priority: relay push (TypeID=0x42, sent just before LobbyInitialized)
                    //           > %TEMP% file (relay wrote it, or same-machine fallback).
                    // In cross-network play only the relay-push path is reliable — the file
                    // only exists on the relay host, not on the remote client.
                    string p2Source;
                    string p2Json = DeckCache.ResolveP2(out p2Source);
                    if (p2Json == null && File.Exists(text3))
                    {
                        p2Json = File.ReadAllText(text3);
                        p2Source = "file";
                    }
                    if (p2Json != null)
                    {
                        game.Settings.P2.Deck = JsonConvert.DeserializeObject<BattleDeck>(p2Json);
                        text = text + "P2 deck faction=" + game.Settings.P2.Deck.FactionId.ToString()
                               + " src=" + p2Source + "\n";
                    }

                    // ── P1 PlayerInfo (relay push TypeID=0x43) ────────────────────────────
                    // The relay broadcasts the native PlayerInfo JSON from each client's
                    // PlayerInitialized Params["PlayerInfo"] — the same JSON the game
                    // already serialises in AppMatchmakingGOGState.OnStartMatchmaking via
                    // JsonConvert.SerializeObject(PlayerInfoFactory.GOG()).
                    // Shape: {Name, Title, Personality:{PersonalityName}, Level, MMR, Rank,
                    //         Vanity:{AvatarId, TauntPackId, BorderId, TitleId, BoardId}}
                    if (PlayerInfoCache.P1InfoJson != null)
                    {
                        try
                        {
                            PlayerInfo pi = JsonConvert.DeserializeObject<PlayerInfo>(PlayerInfoCache.P1InfoJson);
                            if (pi != null)
                            {
                                game.Settings.P1.Info = pi;
                                text += "P1 info name=" + (pi.Name ?? "") + " avatar=" + (pi.Vanity != null ? pi.Vanity.AvatarId.ToString() : "null") + "\n";
                            }
                        }
                        catch (Exception exI) { text += "P1 info deserialize error: " + exI.Message + "\n"; }
                    }
                    else
                    {
                        text += "P1 info: no relay push received (names/avatars will be default)\n";
                    }

                    // ── P2 PlayerInfo (relay push TypeID=0x43) ────────────────────────────
                    if (PlayerInfoCache.P2InfoJson != null)
                    {
                        try
                        {
                            PlayerInfo pi = JsonConvert.DeserializeObject<PlayerInfo>(PlayerInfoCache.P2InfoJson);
                            if (pi != null)
                            {
                                game.Settings.P2.Info = pi;
                                text += "P2 info name=" + (pi.Name ?? "") + " avatar=" + (pi.Vanity != null ? pi.Vanity.AvatarId.ToString() : "null") + "\n";
                            }
                        }
                        catch (Exception exI) { text += "P2 info deserialize error: " + exI.Message + "\n"; }
                    }
                    else
                    {
                        text += "P2 info: no relay push received (names/avatars will be default)\n";
                    }

                    EPlayerId playerID = (EPlayerId)netLobby.CurrentUser.PlayerID;
                    string pidPath = Path.Combine(Path.GetTempPath(), "gwent_patch_trace_pid" + Process.GetCurrentProcess().Id.ToString() + ".log");
                    text = text + "netLobby.CurrentUser.PlayerID raw int = " + netLobby.CurrentUser.PlayerID.ToString() + "\n";
                    text = text + "playerID enum = " + playerID.ToString() + "\n";
                    text = text + "CurrentUser.ServiceID = " + netLobby.CurrentUser.ServiceID.ToString() + "\n";
                    if (isHuman)
                    {
                        if (playerID == EPlayerId.P1)
                        {
                            typeof(GameInstance).GetProperty("GameMode").GetSetMethod(true).Invoke(game, new object[] { EGameMode.Server });
                            GameModeMask.SuppressServerMode = true;
                            text += "Reflected GameMode -> Server (this is P1), SuppressServerMode=true\n";
                        }
                        else
                        {
                            text = text + "Did NOT reflect GameMode (this is " + playerID.ToString() + ")\n";
                        }
                        game.GameController.SetupHumanVsHuman(playerID);
                        text = text + "GameMode after = " + game.GameMode.ToString() + "\n";
                        text = text + "HasAuthority   = " + game.HasAuthority.ToString() + "\n";
                        text = text + "PlayerManager.LocalPlayerId = " + game.GameController.PlayerManager.LocalPlayerId.ToString() + "\n";
                        if (game.GameController.PlayerManager.LocalPlayer != null)
                        {
                            text = text + "PlayerManager.LocalPlayer.Id = " + game.GameController.PlayerManager.LocalPlayer.Id.ToString() + "\n";
                        }
                        else
                        {
                            text += "PlayerManager.LocalPlayer == null\n";
                        }
                        game.GameController.EventManager.OnGameStateChanged.AddListener(delegate(AGameState from, AGameState to)
                        {
                            try
                            {
                                GameController gameController = game.GameController;
                                string text4 = string.Concat(new string[]
                                {
                                    "\nSTATE ",
                                    (from == null) ? "null" : from.GameStateId.ToString(),
                                    " -> ",
                                    (to == null) ? "null" : to.GameStateId.ToString(),
                                    " | curPlayer=",
                                    gameController.PlayerManager.CurrentPlayerId.ToString(),
                                    " | LogicPaused=",
                                    gameController.IsLogicPaused.ToString(),
                                    " | hasActions=",
                                    gameController.ActionManager.HasActions.ToString(),
                                    " | hasRequests=",
                                    gameController.RequestManager.HasRequests.ToString(),
                                    " | hasAbilities=",
                                    gameController.AbilityManager.HasInstances.ToString(),
                                    " | execStack=",
                                    gameController.ExecutionStack.HasCards.ToString(),
                                    " | playStack=",
                                    gameController.PlayStack.HasCards.ToString(),
                                    "\n"
                                });
                                File.AppendAllText(pidPath, text4);
                            }
                            catch (Exception ex2)
                            {
                                try
                                {
                                    string pidPath4 = pidPath;
                                    string text5 = "OGSC ex: ";
                                    Exception ex3 = ex2;
                                    File.AppendAllText(pidPath4, text5 + ((ex3 != null) ? ex3.ToString() : null) + "\n");
                                }
                                catch
                                {
                                }
                            }
                        }, 999);
                        game.GameController.EventManager.OnPlayedCard.AddListener(delegate(EPlayerId pid, Card card)
                        {
                            try
                            {
                                // A genuine card play (Card.Play) resets the consecutive
                                // -timeout streak so the 3-strikes forfeit only counts
                                // CONSECUTIVE idle turns. Forced discards never fire here.
                                Patch_RequestPlayCardAction_ForceResolve.NotePlayedCard(pid);
                                GameController gameController2 = game.GameController;
                                File.AppendAllText(pidPath, string.Concat(new string[]
                                {
                                    "\nPLAYED player=",
                                    pid.ToString(),
                                    " card=",
                                    (card == null) ? "null" : (card.Id.ToString() + "(t" + card.Template.Id.ToString() + ")"),
                                    " | LogicPaused=",
                                    gameController2.IsLogicPaused.ToString(),
                                    " | hasActions=",
                                    gameController2.ActionManager.HasActions.ToString(),
                                    " | hasRequests=",
                                    gameController2.RequestManager.HasRequests.ToString(),
                                    " | hasAbilities=",
                                    gameController2.AbilityManager.HasInstances.ToString(),
                                    " | execStack=",
                                    gameController2.ExecutionStack.HasCards.ToString(),
                                    " | playStack=",
                                    gameController2.PlayStack.HasCards.ToString(),
                                    "\n"
                                }));
                                if (card != null)
                                {

                                    List<ARequestAction> requests = gameController2.RequestManager.Requests;
                                    File.AppendAllText(pidPath, "  requests.Count=" + requests.Count.ToString() + "\n");
                                    MethodInfo setMethod = typeof(RequestPlayCardAction).GetProperty("CardIdPlayed", BindingFlags.Instance | BindingFlags.Public).GetSetMethod(true);
                                    for (int i = requests.Count - 1; i >= 0; i--)
                                    {
                                        ARequestAction arequestAction = requests[i];
                                        File.AppendAllText(pidPath, string.Concat(new string[]
                                        {
                                            "  req[",
                                            i.ToString(),
                                            "] type=",
                                            arequestAction.GetType().Name,
                                            " PlayerId=",
                                            arequestAction.PlayerId.ToString(),
                                            " IsFulfilled=",
                                            arequestAction.IsFulfilled().ToString(),
                                            "\n"
                                        }));
                                        RequestPlayCardAction requestPlayCardAction = arequestAction as RequestPlayCardAction;
                                        if (requestPlayCardAction != null && requestPlayCardAction.PlayerId == pid && !requestPlayCardAction.IsFulfilled())
                                        {
                                            setMethod.Invoke(requestPlayCardAction, new object[] { card.Id });
                                            File.AppendAllText(pidPath, string.Concat(new string[]
                                            {
                                                "  ORPHAN-FULFILL reqId=",
                                                requestPlayCardAction.Id.ToString(),
                                                " cardId=",
                                                card.Id.ToString(),
                                                "\n"
                                            }));
                                        }
                                    }
                                }
                            }
                            catch (Exception ex4)
                            {
                                try
                                {
                                    string pidPath2 = pidPath;
                                    string text6 = "OPC ex: ";
                                    Exception ex5 = ex4;
                                    File.AppendAllText(pidPath2, text6 + ((ex5 != null) ? ex5.ToString() : null) + "\n");
                                }
                                catch
                                {
                                }
                            }
                        }, 0);
                        game.GameController.EventManager.OnTurnEnded.AddListener(delegate(EPlayerId pid)
                        {
                            try
                            {
                                File.AppendAllText(pidPath, "\nTURN ENDED player=" + pid.ToString() + "\n");
                                // Turn is over: clear the timeout suppression so the next
                                // turn's dirty-handling is normal again.
                                Patch_RequestPlayCardAction_ForceResolve._suppressFinishedHandshake = false;
                            }
                            catch
                            {
                            }
                        }, 0);
                        game.GameController.EventManager.OnTurnStarted.AddListener(delegate(EPlayerId pid)
                        {
                            try
                            {
                                File.AppendAllText(pidPath, "\nTURN STARTED player=" + pid.ToString() + "\n");
                            }
                            catch
                            {
                            }
                        }, 0);
                        GameInstance gameForTick = game;
                        string tickPath = pidPath;
                        // Per-match reset + dispose any TICK timer from a previous match so
                        // old games stop ticking (was a leak: two games' timers wrote to one
                        // log, producing the bogus Results<->Turn alternation).
                        Patch_RequestPlayCardAction_ForceResolve.ResetForNewMatch();
                        if (_tickTimer != null) { try { _tickTimer.Dispose(); } catch { } _tickTimer = null; }
                        _tickTimer = new Timer(delegate(object _)
                        {
                            try
                            {
                                if (gameForTick != null && gameForTick.GameController != null)
                                {
                                    GameController gameController3 = gameForTick.GameController;
                                    AGameState agameState = gameController3.StateMachine.GetCurrentState() as AGameState;
                                    StringBuilder stringBuilder = new StringBuilder();
                                    stringBuilder.Append("TICK state=").Append((agameState == null) ? "null" : agameState.GameStateId.ToString()).Append(" curPlayer=")
                                        .Append(gameController3.PlayerManager.CurrentPlayerId)
                                        .Append(" | LogicPaused=")
                                        .Append(gameController3.IsLogicPaused)
                                        .Append(" | hasActions=")
                                        .Append(gameController3.ActionManager.HasActions)
                                        .Append(" | hasReq=")
                                        .Append(gameController3.RequestManager.HasRequests)
                                        .Append(" | reqCount=")
                                        .Append(gameController3.RequestManager.Requests.Count)
                                        .Append(" | hasAb=")
                                        .Append(gameController3.AbilityManager.HasInstances)
                                        .Append(" | exec=")
                                        .Append(gameController3.ExecutionStack.HasCards)
                                        .Append(" | play=")
                                        .Append(gameController3.PlayStack.HasCards)
                                        .Append(" | pauseReqs=")
                                        .Append(gameController3.PauseRequests.Count)
                                        .Append(" | CanAdv=")
                                        .Append(gameController3.CanAdvanceState)
                                        .Append(" | gcIsFin(true)=")
                                        .Append(gameController3.IsFinished(true))
                                        .Append("\n");
                                    List<ARequestAction> requests2 = gameController3.RequestManager.Requests;
                                    for (int j = 0; j < requests2.Count; j++)
                                    {
                                        ARequestAction arequestAction2 = requests2[j];
                                        if (arequestAction2 != null)
                                        {
                                            stringBuilder.Append("    req[").Append(j).Append("] ")
                                                .Append(arequestAction2.GetType().Name)
                                                .Append(" PlayerId=")
                                                .Append(arequestAction2.PlayerId)
                                                .Append(" IsFulfilled=")
                                                .Append(arequestAction2.IsFulfilled())
                                                .Append(" Expired=")
                                                .Append(arequestAction2.Expired)
                                                .Append(" Id=")
                                                .Append(arequestAction2.Id);
                                            try
                                            {
                                                Player player = gameController3.PlayerManager.GetPlayer(arequestAction2.PlayerId);
                                                if (player != null)
                                                {
                                                    stringBuilder.Append(" pIsLocal=").Append(player.IsLocal()).Append(" pStatus=")
                                                        .Append(player.Status);
                                                }
                                            }
                                            catch
                                            {
                                            }
                                            RequestPlayCardAction requestPlayCardAction2 = arequestAction2 as RequestPlayCardAction;
                                            if (requestPlayCardAction2 != null)
                                            {
                                                stringBuilder.Append(" TargetPid=").Append(requestPlayCardAction2.TargetPlayer).Append(" CardToPlay=")
                                                    .Append(requestPlayCardAction2.CardIdToPlay)
                                                    .Append(" CardPlayed=")
                                                    .Append(requestPlayCardAction2.CardIdPlayed)
                                                    .Append(" PlayerPassed=")
                                                    .Append(requestPlayCardAction2.PlayerPassed)
                                                    .Append(" NoOptions=")
                                                    .Append(requestPlayCardAction2.PlayerHasNoOptions);
                                            }
                                            stringBuilder.Append("\n");
                                        }
                                    }
                                    try
                                    {
                                        using (FileStream fileStream = new FileStream(tickPath, FileMode.Append, FileAccess.Write, FileShare.ReadWrite))
                                        {
                                            using (StreamWriter streamWriter = new StreamWriter(fileStream))
                                            {
                                                streamWriter.Write(stringBuilder.ToString());
                                            }
                                        }
                                    }
                                    catch
                                    {
                                    }
                                }
                            }
                            catch (Exception ex6)
                            {
                                try
                                {
                                    using (FileStream fileStream2 = new FileStream(tickPath, FileMode.Append, FileAccess.Write, FileShare.ReadWrite))
                                    {
                                        using (StreamWriter streamWriter2 = new StreamWriter(fileStream2))
                                        {
                                            TextWriter textWriter = streamWriter2;
                                            string text7 = "tick ex: ";
                                            Exception ex7 = ex6;
                                            textWriter.Write(text7 + ((ex7 != null) ? ex7.ToString() : null) + "\n");
                                        }
                                    }
                                }
                                catch
                                {
                                }
                            }
                        }, null, 2000, 1000);
                    }
                    else
                    {
                        EPlayerId opponentPlayerId = game.GameController.PlayerManager.GetOpponentPlayerId(playerID);
                        game.GameController.SetupHumanVsAI(opponentPlayerId, playerID, null);
                    }
                    if (onLobbyInitialized != null)
                    {
                        onLobbyInitialized(connector);
                    }
                    GameInstance gameRef = game;
                    ASingleton<GwentApp>.Instance.OnGameInstanceStarted(delegate(GameInstance gi)
                    {
                        try
                        {
                            string text8 = "\n--- OnGameInstanceStarted fired ---\n";
                            text8 = text8 + "gi.GameController.PlayerManager.LocalPlayerId = " + gi.GameController.PlayerManager.LocalPlayerId.ToString() + "\n";
                            text8 = text8 + "gi.LocalPlayerId (chained)                    = " + gi.LocalPlayerId.ToString() + "\n";
                            text8 = text8 + "gi == lambda's local game?                    = " + (gi == gameRef).ToString() + "\n";
                            GwentApp instance = ASingleton<GwentApp>.Instance;
                            text8 = text8 + "GwentApp.CurrentGame == gi?                   = " + (instance != null && instance.CurrentGame == gi).ToString() + "\n";
                            if (instance != null && instance.CurrentGame != null)
                            {
                                text8 = text8 + "GwentApp.CurrentGame.LocalPlayerId            = " + instance.CurrentGame.LocalPlayerId.ToString() + "\n";
                            }
                            text8 = text8 + "gi.GameMode                                   = " + gi.GameMode.ToString() + "\n";
                            text8 = text8 + "gi.HasAuthority                               = " + gi.HasAuthority.ToString() + "\n";
                            File.AppendAllText(pidPath, text8);
                        }
                        catch (Exception ex8)
                        {
                            try
                            {
                                string pidPath3 = pidPath;
                                string text9 = "\nException in OGI listener: ";
                                Exception ex9 = ex8;
                                File.AppendAllText(pidPath3, text9 + ((ex9 != null) ? ex9.ToString() : null));
                            }
                            catch
                            {
                            }
                        }
                    }, 999);
                    File.WriteAllText(Path.Combine(Path.GetTempPath(), "gwent_patch_trace_pid" + Process.GetCurrentProcess().Id.ToString() + ".log"), text);
                }
                catch (Exception ex)
                {
                    try
                    {
                        File.WriteAllText(Path.Combine(Path.GetTempPath(), "gwent_patch_error.log"), text + "\n---EXCEPTION---\n" + ex.ToString());
                    }
                    catch
                    {
                    }
                }
            }, false, 1000);

            __result = connector;
            return false; // skip original
        }
    }
    
    // =========================================================================
    // PATCH 5: OnlineNetworkConnector.LobbyMessageHandler fix
    //
    // Direct Prefix replacement of LobbyMessageHandler.
    //
    // Original: uses bundle.Sender.PlayerID as sender, falls back to 0 if
    //           Sender is null — which breaks the relay where Sender is null
    //           and the source player ID is in bundle.TargetPlayerID instead.
    //
    // Patched:  falls back to bundle.TargetPlayerID when Sender is null or
    //           Sender.PlayerID == 0.
    // =========================================================================
    // Patch 5: Postfix OnlineNetworkConnector.Initialize to:
    //   (a) Replace the TypeID=0 executor with fixed sender logic (existing fix).
    //   (b) Register a new executor on TypeID=0x42 to receive the relay-pushed
    //       opponent deck message (Problem 2 fix — cross-network deck exchange).
    //   (c) Register a new executor on TypeID=0x43 to receive the relay-pushed
    //       player info message (vanity fix — avatar/border/title/name for both players).
    //
    // TypeID=0x42 ('B') and 0x43 ('C') are unused by any game command or internal handler.
    // The relay sends 0x42 for deck pushes and 0x43 for PlayerInfo pushes.
    // bytes[3] (TargetPlayerID) = the player slot (1=P1, 2=P2) whose data this is.
    // Payload (bytes[8:]) = UTF-8 JSON string.
    //
    // We avoid patching the private LobbyMessageHandler directly since Harmony
    // cannot reliably detour private methods in Mono/.NET 3.5.
    [HarmonyPatch(typeof(OnlineNetworkConnector))]
    [HarmonyPatch("Initialize")]
    [HarmonyPatch(new Type[] { typeof(GameInstance) })]
    public static class Patch_OnlineNetworkConnector_Initialize
    {
        // TypeID used for relay→client deck-push messages (must match relay.py TYPE_RELAY_DECK_PUSH).
        // Uses TypeID=0x00 (game command) so RedLobbyManager dispatches it to the executor.
        // RedLobbyManager only dispatches TypeID=0x00; custom TypeIDs (0x42, 0x43) are silently dropped.
        internal const byte RELAY_DECK_PUSH_TYPE = 0x00;
        // CommandID: 0xF0 = "here is a deck" (high value to avoid game command conflicts)
        internal const byte RELAY_DECK_CMD = 0xF0;

        // TypeID used for relay→client playerinfo-push messages (must match relay.py TYPE_RELAY_INFO_PUSH).
        internal const byte RELAY_INFO_PUSH_TYPE = 0x00;
        // CommandID: 0xF1 = "here is player info"
        internal const byte RELAY_INFO_CMD = 0xF1;

        static void Postfix(OnlineNetworkConnector __instance)
        {
            try
            {
                // ── (a) Fix LobbyMessageHandler sender detection + deck/info push handling ──
                // All relay messages use TypeID=0x00 so they route through this single executor.
                // CommandID 0xF0 = DeckPush, 0xF1 = InfoPush, 200 = SessionCommand, rest = game commands.
                __instance.Lobby.UnRegisterExecutor(0);
                __instance.Lobby.RegisterExecutor(0, new Action<ILobbyManager, RedBundle>(
                    (mgr, bundle) =>
                    {
                        // ── DeckPush (CommandID=0xF0) ──
                        if (bundle.CommandID == RELAY_DECK_CMD)
                        {
                            try
                            {
                                byte[] payload = bundle.Payload;
                                if (payload == null || payload.Length == 0) return;
                                string deckJson = System.Text.Encoding.UTF8.GetString(payload, 0, payload.Length);
                                int playerSlot = bundle.TargetPlayerID;
                                if (playerSlot == 1)
                                {
                                    DeckCache.P1DeckJson = deckJson;
                                    Patch_AppMatchmakingGOGState_OnStartMatchmaking.Captured = false;
                                }
                                else if (playerSlot == 2)
                                    DeckCache.P2DeckJson = deckJson;
                                try
                                {
                                    string tmp = Path.GetTempPath();
                                    if (playerSlot == 1)
                                        File.WriteAllText(Path.Combine(tmp, "gwent_relay_deck_0.json"), deckJson);
                                    else if (playerSlot == 2)
                                        File.WriteAllText(Path.Combine(tmp, "gwent_relay_deck_1.json"), deckJson);
                                }
                                catch { }
                                MelonLogger.Msg(string.Format("[DeckPush] Received P{0} deck from relay ({1} bytes)", playerSlot, deckJson.Length));
                            }
                            catch (Exception ex2) { MelonLogger.Error("[DeckPush] error: " + ex2.Message); }
                            return;
                        }
                        // ── InfoPush (CommandID=0xF1) ──
                        if (bundle.CommandID == RELAY_INFO_CMD)
                        {
                            try
                            {
                                byte[] payload = bundle.Payload;
                                if (payload == null || payload.Length == 0) return;
                                string infoJson = System.Text.Encoding.UTF8.GetString(payload, 0, payload.Length);
                                int playerSlot = bundle.TargetPlayerID;
                                if (playerSlot == 1)
                                    PlayerInfoCache.P1InfoJson = infoJson;
                                else if (playerSlot == 2)
                                    PlayerInfoCache.P2InfoJson = infoJson;
                                MelonLogger.Msg(string.Format("[InfoPush] Received P{0} PlayerInfo from relay ({1} bytes)", playerSlot, infoJson.Length));
                            }
                            catch (Exception ex2) { MelonLogger.Error("[InfoPush] error: " + ex2.Message); }
                            return;
                        }
                        // ── SessionCommand ──
                        if (bundle.CommandID == 200)
                        {
                            SessionCommand sessionCommand = new SessionCommand(bundle);
                            __instance.SessionCommandReceived.Invoke(sessionCommand);
                            return;
                        }
                        // ── Normal game command ──
                        GwentCommand gwentCommand = new GwentCommand(bundle);
                        if (bundle.Sender != null && bundle.Sender.PlayerID != 0)
                        {
                            __instance.ExecuteCommand(gwentCommand.Message, (EPlayerId)bundle.Sender.PlayerID);
                            return;
                        }
                        __instance.ExecuteCommand(gwentCommand.Message, (EPlayerId)bundle.TargetPlayerID);
                    }));
            }
            catch (Exception ex)
            {
                MelonLogger.Error("[Patch5] Initialize Postfix (executor 0) error: " + ex.Message);
            }

            // DeckPush (0xF0) and InfoPush (0xF1) are now handled inline in the
            // TypeID=0x00 executor above — no separate executor registration needed.
        }
    }

    // =========================================================================
    // PATCH 6: ABattleEvent<T> VisualEvent fix — GameInstance.get_GameMode mask
    //
    // The ABattleEvent<T> constructor skips VisualEvent creation when
    // GameMode == Server. C1 gets reflected to Server before card spawning,
    // so all ABattleEvent instances created during SpawnCardsAction have no
    // VisualEvent, breaking visuals.
    //
    // Fix: patch GameInstance.get_GameMode to return Client whenever
    // SuppressServerMode is true. We set this flag in CreateClientConnector
    // around SetupHumanVsHuman, and it stays true for the session so that
    // all subsequent ABattleEvent construction (card spawning etc.) sees Client.
    //
    // This is safe because HasAuthority derives from GameMode == Server, but
    // HasAuthority is a separate property we leave unpatched — authority logic
    // continues to work correctly via HasAuthority while visuals see Client.
    // =========================================================================
    public static class GameModeMask
    {
        public static bool SuppressServerMode = false;

        // Read the backing field directly, bypassing our get_GameMode patch
        private static readonly FieldInfo _gameModeField =
            typeof(GameInstance).GetField("<GameMode>k__BackingField", BindingFlags.Instance | BindingFlags.NonPublic);

        public static EGameMode GetRealGameMode(GameInstance instance)
        {
            if (_gameModeField != null)
                return (EGameMode)_gameModeField.GetValue(instance);
            return instance.GameMode; // fallback
        }
    }

    [HarmonyPatch(typeof(GameInstance))]
    [HarmonyPatch("get_GameMode")]
    public static class Patch_GameInstance_GetGameMode
    {
        static void Postfix(ref EGameMode __result)
        {
            if (GameModeMask.SuppressServerMode && __result == EGameMode.Server)
                __result = EGameMode.Client;
        }
    }

    [HarmonyPatch(typeof(GameInstance))]
    [HarmonyPatch("get_HasAuthority")]
    public static class Patch_GameInstance_GetHasAuthority
    {
        static bool Prefix(GameInstance __instance, ref bool __result)
        {
            if (GameModeMask.SuppressServerMode)
            {
                EGameMode real = GameModeMask.GetRealGameMode(__instance);
                __result = real == EGameMode.SinglePlayer || real == EGameMode.Server;
                return false;
            }
            return true;
        }
    }

    // =========================================================================
    // SSL Certificate Bypass — eliminates Fiddler dependency
    // =========================================================================
    // GOGCertificateValidator.RemoteCertificateValidationCallback is added to
    // ServicePointManager via Delegate.Combine in DotNetRestClientHelper.ConfigureService().
    // It rejects the self-signed fake.crt because CheckIntegrity fails and
    // CheckKnownCertificates doesn't match the GOG cert hash.
    // Patching it to always return true makes all HTTPS calls succeed without Fiddler.
    // The class is internal, so we use AccessTools to target it.
    // =========================================================================
    public static class Patch_GOGCertificateValidator
    {
        public static void Apply(HarmonyLib.Harmony harmony)
        {
            // Patch the validator method itself
            var targetMethod = AccessTools.Method(
                "GwentWebServiceClient.Services.GOGRest.GOGCertificateValidator:RemoteCertificateValidationCallback");
            if (targetMethod == null)
            {
                MelonLogger.Warning("[CertBypass] Could not find GOGCertificateValidator.RemoteCertificateValidationCallback");
                return;
            }
            var prefix = AccessTools.Method(typeof(Patch_GOGCertificateValidator), "Prefix");
            harmony.Patch(targetMethod, prefix: new HarmonyMethod(prefix));

            // Also patch ConfigureService to override the callback AFTER the game sets it
            var configMethod = AccessTools.Method(
                "GwentWebServiceClient.Services.GOGRest.DotNetRestClientHelper:ConfigureService");
            if (configMethod != null)
            {
                var postfix = AccessTools.Method(typeof(Patch_GOGCertificateValidator), "ConfigureServicePostfix");
                harmony.Patch(configMethod, postfix: new HarmonyMethod(postfix));
                MelonLogger.Msg("[CertBypass] DotNetRestClientHelper.ConfigureService postfixed.");
            }

            // Belt-and-suspenders: set it right now too
            System.Net.ServicePointManager.ServerCertificateValidationCallback = AcceptAll;
            System.Net.ServicePointManager.SecurityProtocol |= (System.Net.SecurityProtocolType)3072; // TLS 1.2

            MelonLogger.Msg("[CertBypass] GOGCertificateValidator patched.");
        }

        static bool Prefix(ref bool __result)
        {
            __result = true;
            return false; // skip original
        }

        static void ConfigureServicePostfix()
        {
            // After the game's ConfigureService adds GOGCertificateValidator via Delegate.Combine,
            // replace the entire callback chain with our accept-all.
            System.Net.ServicePointManager.ServerCertificateValidationCallback = AcceptAll;
            System.Net.ServicePointManager.SecurityProtocol |= (System.Net.SecurityProtocolType)3072; // TLS 1.2
        }

        private static bool AcceptAll(object sender, System.Security.Cryptography.X509Certificates.X509Certificate cert,
            System.Security.Cryptography.X509Certificates.X509Chain chain, System.Net.Security.SslPolicyErrors errors)
        {
            return true;
        }
    }

    // =========================================================================
    // CROWN REPORTING: report each player's final crown count to the relay
    //
    // The relay/server cannot otherwise know how many rounds each player won
    // (the full game log with per-player crowns is never POSTed to the server,
    // and the relay can only see the match Winner from EndGameAction, not the
    // loser's exact crowns -- draw-rounds make round-counting ambiguous).
    //
    // Only the AUTHORITY client (P1/C1, the one whose GameMode was reflected to
    // Server, i.e. GameModeMask.SuppressServerMode == true) has the real engine
    // state for BOTH players, so only it sends the report. The relay maps
    // P1 -> service_id_1 and P2 -> service_id_2 and awards crowns = actual
    // rounds won (0/1/2) per player.
    //
    // Wire: an IGameCommand with TypeID=0x00 and CommandID=0xF2 whose Message is
    // a small JSON string {"p1":<crowns>,"p2":<crowns>,"winner":<1|2|3>}. The
    // relay intercepts CommandID==0xF2 in its pipe() and does NOT forward it.
    // CrownsReportCommand mirrors GwentGameplay.SessionCommand's serialization
    // ([Channel byte][UTF-16 message]) so the relay reads it from bundle.Payload.
    // =========================================================================
    public class CrownsReportCommand : IGameCommand, ICommand
    {
        public const byte CMD_CROWNS_REPORT = 0xF2;  // must match CMD_RELAY_CROWNS in relay.py

        public CrownsReportCommand(string message)
        {
            this.Message = message;
            this.Channel = ActionChannel.Default;
        }

        public byte TypeID { get { return 0; } }
        public byte CommandID { get { return CMD_CROWNS_REPORT; } }
        public bool ShouldResend { get { return false; } }
        public string Message { get; private set; }
        public ActionChannel Channel { get; set; }

        public byte[] GetBytes()
        {
            // [Channel byte][UTF-16LE message] -- identical layout to SessionCommand.
            byte[] msg = Encoding.Unicode.GetBytes(this.Message);
            byte[] outBytes = new byte[msg.Length + 1];
            outBytes[0] = (byte)this.Channel;
            Array.Copy(msg, 0, outBytes, 1, msg.Length);
            return outBytes;
        }
    }

    [HarmonyPatch(typeof(AppGameOutcomeState))]
    [HarmonyPatch("GatherCombatResults")]
    public static class Patch_AppGameOutcomeState_GatherCombatResults
    {
        // Guard so we report at most once per match even if GatherCombatResults
        // is somehow invoked more than once.
        private static int _lastReportedGameLogId = int.MinValue;

        static void Postfix(AppGameOutcomeState __instance)
        {
            try
            {
                // Only the authority client knows both players' real crowns.
                if (!GameModeMask.SuppressServerMode)
                    return;

                GameController gc = __instance.GameController;
                if (gc == null || gc.PlayerManager == null)
                    return;

                Player p1 = gc.PlayerManager.GetPlayer(EPlayerId.P1);
                Player p2 = gc.PlayerManager.GetPlayer(EPlayerId.P2);
                if (p1 == null || p2 == null)
                    return;

                int p1Crowns = p1.Crowns;
                int p2Crowns = p2.Crowns;

                // Winner as an int bitflag: 1=P1, 2=P2, 3=both (draw/double-win).
                int winner = (int)__instance.Winner;

                // Dedupe by GameLogId when available.
                int gameLogId = __instance.GameLogId.HasValue ? (int)__instance.GameLogId.Value : 0;
                if (gameLogId != 0 && gameLogId == _lastReportedGameLogId)
                    return;
                _lastReportedGameLogId = gameLogId;

                string json = "{\"p1\":" + p1Crowns + ",\"p2\":" + p2Crowns +
                              ",\"winner\":" + winner + ",\"game_id\":" + gameLogId + "}";

                if (gc.Network != null)
                {
                    gc.Network.SendCommand(new CrownsReportCommand(json), Target.Server, (EPlayerId)0);
                    MelonLogger.Msg("[CrownsReport] Sent to relay: " + json);
                }
                else
                {
                    MelonLogger.Warning("[CrownsReport] gc.Network is null; cannot report crowns");
                }
            }
            catch (Exception ex)
            {
                MelonLogger.Error("[CrownsReport] error: " + ex.Message);
            }
        }
    }






    // =========================================================================
    // PATCH: RequestPlayCardAction.ForceResolve  (authority-only timeout handler)
    // =========================================================================
    // On a turn timeout the vanilla RequestManager.Update calls ForceResolve on the
    // current player's play-request. For a genuine idle (no card queued) vanilla
    // discards a random card and pushes a CancelRequestAction that only removes ONE of
    // the two same-Id play-request mirrors -> HasRequests stays true -> turn never ends
    // (stall); and the player-finished handshake then deadlocks on a remote status report
    // that never arrives. Fix (see TIMEOUT_FIX_DESIGN_v16.md):
    //   (A) NotifyPlayerHasNoOptionsAction(pid) x2 -> PlayerHasNoOptions=true -> both
    //       mirrors removed via the normal networked HandleFulfilled path (no residue).
    //   (B) Arm _suppressFinishedHandshake; Patch_TurnGameState_OnUpdate clears IsDirty so the
    //       turn ends via the NON-dirty path (no RequestPlayerFinished handshake).
    //   (C) Track consecutive timeouts in a static dict; forfeit at the cap.
    public static class Patch_RequestPlayCardAction_ForceResolve
    {
        public const int MAX_CONSECUTIVE_TIMEOUTS = 3;

        // Both mirrors expire in the same RequestManager.Update pass -> ForceResolve runs
        // twice. Dedupe by request Id so we act exactly once per timeout.
        private static int _lastHandledReqId = -1;

        // Per-player consecutive-timeout streak. Reset by NotePlayedCard on a real play.
        private static readonly Dictionary<EPlayerId, int> _streak = new Dictionary<EPlayerId, int>();

        // Set by the postfix on a timeout; consumed by the deterministic
        // Patch_AGameState_HandleDirtyGameController prefix to take the non-dirty
        // turn-end path (no RequestPlayerFinishedAction -> no deadlock). Reset when the
        // Turn state is left (OnTurnEnded) or a real card is played.
        public static bool _suppressFinishedHandshake = false;

        public static void ApplyManually(HarmonyLib.Harmony harmony)
        {
            var original = AccessTools.Method(typeof(RequestPlayCardAction), "ForceResolve");
            var postfix = AccessTools.Method(typeof(Patch_RequestPlayCardAction_ForceResolve), "Postfix");
            harmony.Patch(original, postfix: new HarmonyMethod(postfix));
        }

        // A genuine Card.Play resets the streak and clears the per-turn dedupe guard.
        public static void NotePlayedCard(EPlayerId pid)
        {
            _streak[pid] = 0;
            _lastHandledReqId = -1;
            _suppressFinishedHandshake = false;
        }

        // Full per-match reset (called from LobbyInitialized so a new game starts clean).
        public static void ResetForNewMatch()
        {
            _streak.Clear();
            _lastHandledReqId = -1;
            _suppressFinishedHandshake = false;
        }

        private static void Trace(string msg)
        {
            try
            {
                string pidPath = Path.Combine(Path.GetTempPath(),
                    "gwent_patch_trace_pid" + Process.GetCurrentProcess().Id.ToString() + ".log");
                File.AppendAllText(pidPath, msg + "\n");
            }
            catch { }
        }

        static void Postfix(RequestPlayCardAction __instance)
        {
            try
            {
                GameController gc = __instance.GameController;
                if (gc == null || !gc.HasAuthority) return;

                // Only the genuine-idle path. If a card was queued (CardIdToPlay != 0)
                // vanilla PlayCardOnRandomPosition handled it; if already fulfilled or the
                // player passed (empty-hand branch), leave it to vanilla.
                if (__instance.CardIdToPlay != 0) return;
                if (__instance.IsFulfilled()) return;
                if (__instance.PlayerPassed) return;

                int reqId = __instance.Id;
                if (reqId == _lastHandledReqId) return;   // second mirror in same pass
                _lastHandledReqId = reqId;

                EPlayerId pid = __instance.PlayerId;

                // (A) Clear BOTH play-request mirrors via the engine-native fulfill path.
                //     ApplyImpl clears the first play-request for the player; the pair share
                //     an Id but are two list entries, so push twice (the second no-ops once
                //     the first mirror is gone).
                for (int i = 0; i < 2; i++)
                {
                    NotifyPlayerHasNoOptionsAction noOpt = gc.ActionManager
                        .CreateAction<NotifyPlayerHasNoOptionsAction>().Init(pid);
                    gc.ActionManager.PushAction(noOpt, false);
                }

                // (B) Arm the IsDirty-bypass so the turn ends without the finished-handshake.
                _suppressFinishedHandshake = true;

                // (C) Consecutive-timeout streak + forfeit.
                int s;
                _streak.TryGetValue(pid, out s);
                s += 1;
                _streak[pid] = s;

                Trace("[TimeoutEndTurn] pid=" + pid.ToString() + " reqId=" + reqId.ToString()
                      + " streak=" + s.ToString());

                if (s >= MAX_CONSECUTIVE_TIMEOUTS)
                {
                    EPlayerId opponent = gc.PlayerManager.GetOpponentPlayerId(pid);
                    EndGameAction end = gc.ActionManager
                        .CreateAction<EndGameAction>().Init(opponent, EEndGameReason.PlayerForfeit);
                    gc.ActionManager.ApplyAction(end);
                    _streak[pid] = 0;
                    // keep _suppressFinishedHandshake = true: the game is switching to
                    // Results via EndGameAction; we must NOT let the dirty path create a
                    // RequestPlayerFinishedAction for the timed-out (losing) player, which
                    // would leave it deadlocked/torn-down before the Results/crowns flow.
                    Trace("[TimeoutForfeit] pid=" + pid.ToString() + " winner=" + opponent.ToString());
                }
            }
            catch (Exception ex)
            {
                Trace("[TimeoutEndTurn] ex: " + ex.ToString());
            }
        }
    }

    // =========================================================================
    // PATCH: TurnGameState.OnUpdate  (prefix — IsDirty bypass)
    // =========================================================================
    // Runs BEFORE TurnGameState.OnUpdate (hence before AGameState.OnUpdate checks IsDirty).
    // When the ForceResolve postfix has armed _suppressFinishedHandshake AND the play-requests
    // are gone (IsFinished(false) == true), clear IsDirty so the turn ends through the
    // non-dirty path: SetAllPlayersStatus(Finished) -> OnFinished -> SwitchGameState(TurnEnd).
    // No RequestPlayerFinishedAction is created, so the remote-status handshake (and its
    // deadlock / hash-validation desync) never happens.
    public static class Patch_TurnGameState_OnUpdate
    {
        public static void ApplyManually(HarmonyLib.Harmony harmony)
        {
            var original = AccessTools.Method(typeof(TurnGameState), "OnUpdate");
            var postfix = AccessTools.Method(typeof(Patch_TurnGameState_OnUpdate), "Postfix");
            // Only the unstick POSTFIX now. The old IsDirty PREFIX was frame-racy and is
            // replaced by the deterministic Patch_AGameState_HandleDirtyGameController.
            harmony.Patch(original, postfix: new HarmonyMethod(postfix));
        }

        // --- Self-healing turn-start unstick (attempt #17) -------------------------
        // After a timeout, a turn can hang at turn-start on a stuck RequestPlayerReadyAction:
        // the engine is waiting for some player to report Ready (Status==Ready), but that
        // status report doesn't settle after a timeout, so the ready-request never fulfils
        // (it ends up Expired/Blocked) and HasRequests stays true -> the turn can't complete.
        //
        // CRITICAL: the fulfil condition for RequestPlayerReadyAction is Status==READY, NOT
        // Active. The earlier version forced SetPlayersActive() (Status=Active) which can
        // NEVER fulfil a ready-request -> it orphaned the request and stalled. The correct,
        // engine-native unstick is to set the stuck player's status to READY (networked
        // SetPlayerStatusAction). Then RequestManager.Update fulfils + removes the
        // ready-request, and AGameState.OnUpdate's own "AreAllPlayers(Ready) ->
        // SetPlayersActive()" fires, completing the handshake exactly as a healthy turn does
        // (Active + timer started). We act on WHICHEVER player owns the stuck ready-request
        // (it may be the opponent, e.g. P2's ready-request during P1's turn).
        private const int STUCK_TICKS = 45;
        private static int _stuckTurnIndex = -1;   // RoundInfo.TurnIndex we last acted on
        private static int _stuckCounter = 0;
        private static int _watchTurnIndex = -2;   // turn currently being watched

        static void Postfix(TurnGameState __instance)
        {
            try
            {
                GameController gc = __instance.GameController;
                if (gc == null || !gc.HasAuthority) return;
                if (gc.StateMachine.GetCurrentStateID() != (int)EGameStateId.Turn) return;

                RoundInfo round = gc.RoundManager.CurrentRound;
                int turnIndex = (round != null) ? round.TurnIndex : -1;

                // Find a stuck ready-request (any player). This is the turn-start hang.
                RequestPlayerReadyAction stuckReady = null;
                List<ARequestAction> reqs = gc.RequestManager.Requests;
                for (int i = 0; i < reqs.Count; i++)
                {
                    RequestPlayerReadyAction rr = reqs[i] as RequestPlayerReadyAction;
                    if (rr != null) { stuckReady = rr; break; }
                }

                if (stuckReady == null)
                {
                    // No ready-request outstanding -> nothing to unstick; reset watcher.
                    _watchTurnIndex = -2; _stuckCounter = 0;
                    return;
                }

                // A ready-request exists. Count how long it has persisted this turn.
                if (turnIndex != _watchTurnIndex) { _watchTurnIndex = turnIndex; _stuckCounter = 0; }
                _stuckCounter++;

                if (_stuckCounter >= STUCK_TICKS && turnIndex != _stuckTurnIndex)
                {
                    _stuckTurnIndex = turnIndex;   // act at most once per turn
                    EPlayerId stuckPid = stuckReady.PlayerId;
                    // Force the stuck player to READY via the engine's own networked action.
                    // RequestPlayerReadyAction.IsFulfilled() == (Status==Ready), so this lets
                    // the engine fulfil the request and run SetPlayersActive() itself.
                    SetPlayerStatusAction act = gc.ActionManager
                        .CreateAction<SetPlayerStatusAction>().Init(stuckPid, EPlayerStatus.Ready);
                    gc.ActionManager.ApplyAction(act);
                    _stuckCounter = 0;
                    _watchTurnIndex = -2;
                    try
                    {
                        string pidPath = Path.Combine(Path.GetTempPath(),
                            "gwent_patch_trace_pid" + Process.GetCurrentProcess().Id.ToString() + ".log");
                        File.AppendAllText(pidPath,
                            "[TimeoutUnstick] forced Status=Ready turnIndex=" + turnIndex.ToString()
                            + " stuckPlayer=" + stuckPid.ToString() + "\n");
                    }
                    catch { }
                }
            }
            catch { }
        }

    }

    // =========================================================================
    // PATCH: AGameState.HandleDirtyGameController  (prefix — DETERMINISTIC turn-end)
    // =========================================================================
    // HandleDirtyGameController is the SINGLE gateway to SendPlayerFinishedRequest (the
    // remote finished-handshake that deadlocks after a timeout). Patching this private
    // method as a prefix intercepts the dirty branch at the EXACT call that would create
    // the finished-request -- no frame window to miss (the #16/#17 IsDirty bypass was a
    // per-frame poll that sometimes ran too late). When a timeout has armed the suppress
    // flag and we're in the Turn state: clear IsDirty and SKIP the original, so the engine
    // takes the non-dirty path next Update (SetAllPlayersStatus(Finished) -> OnFinished ->
    // SwitchGameState(TurnEnd)) with NO finished-request and NO hash-validation desync.
    public static class Patch_AGameState_HandleDirtyGameController
    {
        public static void ApplyManually(HarmonyLib.Harmony harmony)
        {
            var original = AccessTools.Method(typeof(AGameState), "HandleDirtyGameController");
            var prefix = AccessTools.Method(typeof(Patch_AGameState_HandleDirtyGameController), "Prefix");
            harmony.Patch(original, prefix: new HarmonyMethod(prefix));
        }

        // Return false to skip the original HandleDirtyGameController.
        static bool Prefix(AGameState __instance)
        {
            try
            {
                if (!Patch_RequestPlayCardAction_ForceResolve._suppressFinishedHandshake)
                    return true;
                GameController gc = __instance.GameController;
                if (gc == null || !gc.HasAuthority) return true;
                // Only intercept the timed-out TURN. Let every other state handle dirty normally.
                if (__instance.GameStateId != EGameStateId.Turn) return true;

                gc.IsDirty = false;   // force the non-dirty path on the next OnUpdate
                try
                {
                    string pidPath = Path.Combine(Path.GetTempPath(),
                        "gwent_patch_trace_pid" + Process.GetCurrentProcess().Id.ToString() + ".log");
                    File.AppendAllText(pidPath,
                        "[TimeoutEndTurn] suppressed finished-handshake (deterministic) -> non-dirty turn end\n");
                }
                catch { }
                return false;   // skip original -> SendPlayerFinishedRequest never called
            }
            catch { return true; }
        }
    
    // ---------------------------------------------------------------
    // Patch: Force ChallengeToDuel + Chat actions on friend contacts
    // ContactExt.RefreshActions requires AccountStatus==HasGwentAccount
    // for ChallengeToDuel, but the vanity lookup may fail or return empty.
    // This postfix ensures friends always get these actions.
    // ---------------------------------------------------------------
    [HarmonyPatch(typeof(ContactExt))]
    [HarmonyPatch("RefreshActions")]
    public static class Patch_ContactExt_RefreshActions
    {
        static void Postfix(ContactExt __instance)
        {
            try
            {
                if (__instance.Friendship != Contact.FriendshipType.Friend)
                    return;
                // Force HasGwentAccount so ViewProfile works too
                if (__instance.AccountStatus != Contact.GwentStatusType.HasGwentAccount)
                    __instance.AccountStatus = Contact.GwentStatusType.HasGwentAccount;

                var actions = __instance.AvaialableActions;
                if (!actions.Contains(ContactActionType.ChallengeToDuel))
                    actions.AddUnique(ContactActionType.ChallengeToDuel);
                if (!actions.Contains(ContactActionType.Chat))
                    actions.AddUnique(ContactActionType.Chat);
                if (!actions.Contains(ContactActionType.ViewProfile))
                    actions.AddUnique(ContactActionType.ViewProfile);
            }
            catch { }
        }
    }

    // ---------------------------------------------------------------
    // Patch: Bypass Galaxy SDK for game invitations (Challenge to Duel).
    // Instead of GalaxyInstance.Friends().SendInvitation() (needs real GOG
    // peer network), POST to our server and call inviteSendSuccess directly.
    // ---------------------------------------------------------------
    [HarmonyPatch(typeof(GOGFriendInviteController))]
    [HarmonyPatch("InviteFriendToGame", new Type[] { typeof(string), typeof(ulong), typeof(Action<ulong>), typeof(Action), typeof(Action) })]
    public static class Patch_GOGFriendInvite_SendInvitation
    {
        // Retrieve GlobalNetworkManager.GwentWebServices.CurrentUserID via reflection
        // because GlobalNetworkManager is internal.
        private static ulong GetMyId()
        {
            try
            {
                var gnmType = typeof(GOGFriendInviteController).Assembly.GetType("GwentUnity.GlobalNetworkManager");
                object gnmInst = null;
                var singletonType = typeof(Singleton<>).MakeGenericType(gnmType);
                gnmInst = singletonType.GetProperty("Instance", BindingFlags.Static | BindingFlags.Public)?.GetValue(null, null);
                if (gnmInst == null) return 0;
                var wsProp = gnmInst.GetType().GetProperty("GwentWebServices", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                var ws = wsProp?.GetValue(gnmInst, null);
                if (ws == null) return 0;
                var idProp = ws.GetType().GetProperty("CurrentUserID", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                return (ulong)(idProp?.GetValue(ws, null) ?? 0UL);
            }
            catch { return 0; }
        }

        static bool Prefix(string lobbyId, ulong opponentId, Action<ulong> inviteSendSuccess, Action inviteSendFail, Action inviteFriendCancel)
        {
            try
            {
                ulong myId = GetMyId();
                string url = "http://127.0.0.1:8447/internal/game_invitations/" + opponentId;
                string bodyStr = "{\"sender_id\":\"" + myId + "\",\"connection_string\":\"" + lobbyId + "\"}";
                var req = (HttpWebRequest)WebRequest.Create(url);
                req.Method = "POST";
                req.ProtocolVersion = System.Net.HttpVersion.Version10;
                req.KeepAlive = false;
                req.Timeout = 10000;
                req.ContentType = "application/json";
                byte[] data = System.Text.Encoding.UTF8.GetBytes(bodyStr);
                req.ContentLength = data.Length;
                using (var stream = req.GetRequestStream())
                    stream.Write(data, 0, data.Length);
                using (var resp = req.GetResponse()) { }
                MelonLoader.MelonLogger.Msg("[GameInvite] Sent invitation to " + opponentId + " lobby=" + lobbyId);
                inviteSendSuccess?.Invoke(opponentId);
            }
            catch (Exception ex)
            {
                MelonLoader.MelonLogger.Warning("[GameInvite] Send failed: " + ex.Message);
                inviteSendFail?.Invoke();
            }
            return false; // skip original
        }
    }

    // ---------------------------------------------------------------
    // Patch: Poll server for pending game invitations instead of
    // waiting for Galaxy SDK GameInvitationReceived native callback.
    // ---------------------------------------------------------------
    [HarmonyPatch(typeof(GOGFriendInviteController))]
    [HarmonyPatch("StartListenToGameInvites")]
    public static class Patch_GOGFriendInvite_StartListening
    {
        private static Thread _pollThread;
        private static volatile bool _polling = false;
        // Friends-sync state: last known server-truth sets. Baselined on the first
        // poll after login (the game loads its own initial state), then diffed each
        // poll to drive the facade's native transition handlers.
        private static HashSet<ulong> _knownFriends = null;
        private static HashSet<ulong> _knownPending = null;

        private static ulong GetMyId()
        {
            try
            {
                var gnmType = typeof(GOGFriendInviteController).Assembly.GetType("GwentUnity.GlobalNetworkManager");
                var singletonType = typeof(Singleton<>).MakeGenericType(gnmType);
                object gnmInst = singletonType.GetProperty("Instance", BindingFlags.Static | BindingFlags.Public)?.GetValue(null, null);
                if (gnmInst == null) return 0;
                var wsProp = gnmInst.GetType().GetProperty("GwentWebServices", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                var ws = wsProp?.GetValue(gnmInst, null);
                if (ws == null) return 0;
                var idProp = ws.GetType().GetProperty("CurrentUserID", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                return (ulong)(idProp?.GetValue(ws, null) ?? 0UL);
            }
            catch { return 0; }
        }

        static void Postfix(GOGFriendInviteController __instance)
        {
            if (_polling) return;
            _polling = true;
            _pollThread = new Thread(() =>
            {
                ulong myId = 0;
                // Wait up to 5s for login to complete
                for (int i = 0; i < 10 && myId == 0; i++) { Thread.Sleep(500); myId = GetMyId(); }
                if (myId == 0) { _polling = false; return; }
                MelonLoader.MelonLogger.Msg("[GameInvite] Polling for invitations as " + myId);
                while (_polling)
                {
                    try
                    {
                        string url = "http://127.0.0.1:8447/internal/game_invitations/pending/" + myId;
                        var req = (HttpWebRequest)WebRequest.Create(url);
                        req.Method = "GET";
                        req.ProtocolVersion = System.Net.HttpVersion.Version10;
                        req.KeepAlive = false;
                        req.Timeout = 5000;
                        string json;
                        using (var resp = req.GetResponse())
                        using (var reader = new System.IO.StreamReader(resp.GetResponseStream()))
                            json = reader.ReadToEnd();
                        var obj = JsonConvert.DeserializeObject<System.Collections.Generic.Dictionary<string, string>>(json);
                        if (obj != null && obj.ContainsKey("inv_id") && !string.IsNullOrEmpty(obj["inv_id"]))
                        {
                            string invId = obj["inv_id"];
                            string senderName = obj.ContainsKey("sender_name") ? obj["sender_name"] : "";
                            MelonLoader.MelonLogger.Msg("[GameInvite] Received invitation inv_id=" + invId + " from=" + senderName);
                            MelonLoader.MelonCoroutines.Start(FireInvitationCoroutine(__instance, senderName, invId));
                        }
                        // ── Friends-list sync (piggybacked on the same response) ──
                        // The server includes the authoritative friends/pending ID sets.
                        // We diff against the last known sets and drive ContactsFacade's
                        // own private handlers (OnFriendAdded / OnInvitationReceived /
                        // OnFriendListChanged) with server-truth data. This deliberately
                        // bypasses the Galaxy SDK friend roster, which is populated once
                        // at login and never refreshed (the reason accepts/deletes used
                        // to require a game restart).
                        if (obj != null && obj.ContainsKey("friends_list"))
                        {
                            var friendsNow = ParseIdSet(obj["friends_list"]);
                            var pendingNow = obj.ContainsKey("pending_list") ? ParseIdSet(obj["pending_list"]) : new HashSet<ulong>();
                            if (_knownFriends == null)
                            {
                                _knownFriends = friendsNow;   // baseline; the game loads its
                                _knownPending = pendingNow;   // own initial state at login
                            }
                            else
                            {
                                var addedFriends = new List<ulong>();
                                foreach (ulong id in friendsNow)
                                    if (!_knownFriends.Contains(id)) addedFriends.Add(id);
                                bool removed = false;
                                foreach (ulong id in _knownFriends)
                                    if (!friendsNow.Contains(id)) { removed = true; break; }
                                var addedPending = new List<ulong>();
                                foreach (ulong id in pendingNow)
                                    if (!_knownPending.Contains(id) && !friendsNow.Contains(id)) addedPending.Add(id);
                                if (addedFriends.Count > 0 || addedPending.Count > 0 || removed)
                                {
                                    MelonLoader.MelonLogger.Msg("[FriendsSync] +" + addedFriends.Count + " friends, +"
                                        + addedPending.Count + " pending, removals=" + removed);
                                    MelonLoader.MelonCoroutines.Start(
                                        ApplyFriendsDeltaCoroutine(addedFriends, addedPending, friendsNow, removed));
                                }
                                _knownFriends = friendsNow;
                                _knownPending = pendingNow;
                            }
                        }
                    }
                    catch (WebException) { /* timeout / no pending */ }
                    catch (Exception ex) { MelonLoader.MelonLogger.Warning("[GameInvite] Poll error: " + ex.Message); }
                    Thread.Sleep(2000);
                }
            }) { IsBackground = true, Name = "GameInvitePoll" };
            _pollThread.Start();
        }

        private static HashSet<ulong> ParseIdSet(string csv)
        {
            var set = new HashSet<ulong>();
            if (string.IsNullOrEmpty(csv)) return set;
            foreach (string part in csv.Split(','))
            {
                ulong id;
                if (ulong.TryParse(part.Trim(), out id) && id != 0) set.Add(id);
            }
            return set;
        }

        // Applies a friends-list delta on the Unity main thread by invoking
        // ContactsFacade's own (private) transition handlers with server-truth data:
        //   OnInvitationReceived(PendingFriends.PendingFriend) — incoming request
        //   OnFriendAdded(PrivateFriends.PrivateFriend)        — accepted (either side)
        //   OnFriendListChanged(PrivateFriends)                — removals (full list diff)
        // These run the game's native MakeContactTransation/RefreshActions logic, so
        // sections and contact actions update exactly as if the SDK had fired them.
        private static System.Collections.IEnumerator ApplyFriendsDeltaCoroutine(
            List<ulong> addedFriends, List<ulong> addedPending, HashSet<ulong> fullFriends, bool removalHappened)
        {
            yield return null;   // hop to the Unity main thread
            try
            {
                var sc = Singleton<SocialController>.Instance;
                if (sc == null || sc.SocialFacade == null)
                {
                    MelonLoader.MelonLogger.Warning("[FriendsSync] SocialController not ready — skipping delta");
                }
                else
                {
                    var contactsProp = sc.SocialFacade.GetType().GetProperty("Contacts",
                        BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                    object facade = contactsProp?.GetValue(sc.SocialFacade, null);
                    if (facade == null)
                    {
                        MelonLoader.MelonLogger.Warning("[FriendsSync] ContactsFacade not found");
                    }
                    else
                    {
                        var ft = facade.GetType();
                        var onInvReceived = ft.GetMethod("OnInvitationReceived", BindingFlags.NonPublic | BindingFlags.Instance);
                        var onFriendAdded = ft.GetMethod("OnFriendAdded", BindingFlags.NonPublic | BindingFlags.Instance);
                        var onListChanged = ft.GetMethod("OnFriendListChanged", BindingFlags.NonPublic | BindingFlags.Instance);
                        foreach (ulong id in addedPending)
                        {
                            if (onInvReceived != null)
                            {
                                onInvReceived.Invoke(facade, new object[] { new PendingFriends.PendingFriend(id) });
                                MelonLoader.MelonLogger.Msg("[FriendsSync] OnInvitationReceived(" + id + ")");
                            }
                        }
                        foreach (ulong id in addedFriends)
                        {
                            if (onFriendAdded != null)
                            {
                                onFriendAdded.Invoke(facade, new object[] { new PrivateFriends.PrivateFriend(id) });
                                MelonLoader.MelonLogger.Msg("[FriendsSync] OnFriendAdded(" + id + ")");
                            }
                        }
                        if (removalHappened && onListChanged != null)
                        {
                            var pf = new PrivateFriends();
                            foreach (ulong id in fullFriends)
                                pf.AddFriend(new PrivateFriends.PrivateFriend(id));
                            onListChanged.Invoke(facade, new object[] { pf });
                            MelonLoader.MelonLogger.Msg("[FriendsSync] OnFriendListChanged(" + fullFriends.Count + " friends)");
                        }
                    }
                }
            }
            catch (Exception ex) { MelonLoader.MelonLogger.Warning("[FriendsSync] delta failed: " + ex.Message); }
        }

        private static System.Collections.IEnumerator FireInvitationCoroutine(GOGFriendInviteController controller, string username, string lobbyId)
        {
            yield return null;
            try
            {
                var method = typeof(GOGFriendInviteController).GetMethod(
                    "OnInvitationReceived",
                    BindingFlags.NonPublic | BindingFlags.Instance);
                if (method != null)
                    method.Invoke(controller, new object[] { username, lobbyId });
                else
                    MelonLoader.MelonLogger.Warning("[GameInvite] Could not find OnInvitationReceived");
            }
            catch (Exception ex) { MelonLoader.MelonLogger.Warning("[GameInvite] FireInvitation error: " + ex.Message); }
        }
    }

    // ---------------------------------------------------------------
    // Patch: capture the chosen deck for FRIEND matches.
    // Casual matchmaking captures via AppMatchmakingGOGState.OnStartMatchmaking,
    // but friend matches go through the PWF states and never hit that hook, so
    // PlayerInitialized carried no Params["Deck"] and the relay had to guess
    // from server-side is_current (often wrong). Both players pass through
    // AppPWFJoinLobbyState.OnEnterState right before JoinCurrentLobby, and the
    // PlayerInitialized wire message is built after the join handshake, so a
    // postfix here injects the deck in time.
    // ---------------------------------------------------------------
    [HarmonyPatch(typeof(AppPWFJoinLobbyState))]
    [HarmonyPatch("OnEnterState")]
    public static class Patch_AppPWFJoinLobbyState_CaptureDeck
    {
        static void Postfix()
        {
            try
            {
                // Reset the once-per-match guard: a friend match may follow a
                // casual match in the same session.
                Patch_AppMatchmakingGOGState_OnStartMatchmaking.Captured = false;
                Patch_AppMatchmakingGOGState_OnStartMatchmaking.CaptureAndInjectDeck("PWF/friend-match");
            }
            catch (Exception ex)
            {
                MelonLoader.MelonLogger.Warning("[DeckCapture] PWF capture failed: " + ex.Message);
            }
        }
    }

    // ---------------------------------------------------------------
    // Patch: "Play with Friend" button (main menu) opens the social
    // panel instead of trying to show the GOG overlay invite dialog,
    // which is unavailable and stalls the game.
    // ---------------------------------------------------------------
    [HarmonyPatch(typeof(PlayWithFriendController))]
    [HarmonyPatch("HandlePlayWithFriendPressed")]
    public static class Patch_PlayWithFriendController_OpenSocialPanel
    {
        static bool Prefix()
        {
            try
            {
                var uiMgr = GwentUIManager.Instance;
                if (uiMgr == null) return true;
                SocialPanel panel = uiMgr.GetExistingPanel<SocialPanel>(PanelID.MenusSocial);
                if (panel != null)
                    panel.Show(false, false);
                else
                {
                    panel = uiMgr.CreatePanel<SocialPanel>(PanelID.MenusSocial, PanelID._Undefined);
                    if (panel != null) panel.Show(false, false);
                }
                return false; // skip original (which would invoke OnPlayWithFriend → stall)
            }
            catch (Exception ex)
            {
                MelonLoader.MelonLogger.Warning("[PlayWithFriend] Social panel open failed: " + ex.Message);
                return true; // fall through to original on error
            }
        }
    }



}
}
