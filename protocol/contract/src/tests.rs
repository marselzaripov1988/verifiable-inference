//! Тесты на свойствах, а не на ветках кода.
//!
//! Числа расхождений взяты из настоящих замеров, а не выдуманы:
//! 37_780 — минимум подлога FP8 (самый незаметный из измеренных),
//! 12_700 — честное среднее по 1520 наблюдениям,
//! 44_660 — худшее честное наблюдение,
//! порог 36_000.
//!
//! Худший случай для протокола — именно 37_780 против порога 36_000:
//! между ними всего 1 780 микродолей. Если правило ломается, оно
//! сломается здесь.

use cosmwasm_std::testing::{mock_dependencies, mock_env, mock_info};
use cosmwasm_std::{coins, from_json, to_json_binary, Addr, Deps, DepsMut, Uint128};

use crate::contract::{commit_hash, execute, instantiate, migrate, query};
use crate::error::ContractError;
use crate::msg::{
    ChecksResponse, ConformanceRef, Cw20ReceiveMsg, ExecuteMsg, InstantiateMsg, JobsResponse,
    MigrateMsg, NextJobResponse, QueryMsg, ReceiveHook,
};
use crate::state::{
    Config, Executor, ExecutorStatus, Forfeit, Job, JobState, MicroLog, Settlement,
    CURRENT_VERSION, STATE_VERSION,
};

const ADMIN: &str = "admin";
const EXEC: &str = "executor1";
const CLIENT: &str = "client1";

const MODEL: &str = "Qwen/Qwen2.5-7B-Instruct";
const RUNTIME: &str = "vllm-0.31.0";
const DTYPE: &str = "bfloat16";
/// Остальная часть класса. Без общего кеша ядер честные узлы расходятся
/// в последнем бите, и равенство хэшей — то есть вердикт — невозможно.
const ARCH: &str = "sm_89";
const CACHE: &str = "3753554454f2c9b850c6ec0912a58e39ee08028012f3de1e9649e62d67cc131a";
// Имена денomов нигде в логике не участвуют — только в настройках.
const STAKE: &str = "uaxm";
const CW20: &str = "cw20token";
const MIN_STAKE: u128 = 50_000_000;
const PRICE: u128 = 1_000_000;
/// Доля проверяющих при PRICE и FEE_BPS: ровно 48 500, без остатка.
const FEE: u128 = 48_500;
/// Сколько вносит ЗАКАЗЧИК: цену плюс долю. Долю платит он, а не
/// исполнитель, — иначе повышение доли молча урезало бы выручку карт.
const TOTAL: u128 = PRICE + FEE;
/// 485 bps. Замер дал 9,7% накладных ПРИ ЧЕТЫРЁХ проверках; проверок
/// теперь две, и вся доля делится между пришедшими
/// (`share = fee_total / checks.len()`), так что при 970 двое получали бы
/// вдвое больше замеренной стоимости — а платил бы это заказчик.
const FEE_BPS: u16 = 485;

/// Адреса доставки. Проверять по ним ничего нельзя и не нужно: контракт
/// смотрит только форму, а содержимое решают хэши.
const ENDPOINT: &str = "http://node1.example:8081";
const PROMPT_URI: &str = "http://client.example:8080";

/// Сколько проверок требуется. Двух хватает: усреднять нечего, внутри
/// класса честные векторы тождественны, а лгущего проверяющего разберёт
/// спор с пересчётом на цепочке.
const CHECKS: u32 = 2;

/// Обязательство исполнителя в заданиях-болванках. Проверка, сдавшая
/// ЭТОТ хэш, совпала с исполнителем.
const COMMIT: &str = "lh";

/// Расхождение как СВИДЕТЕЛЬСТВО, а не как вердикт. Шесть миллионных —
/// настоящее наблюдение: столько даёт разный блок свёртки на Ada, и
/// этого довольно, чтобы побитовое равенство сломалось. Нужно затем,
/// чтобы проверить, что вердикт не зависит от величины.
const TINY: u64 = 6;
const HUGE: u64 = 143_310;
const HONEST_MEAN: u64 = 12_700;

// --- проба на эталонном векторе -----------------------------------------
// Предел НОЛЬ: внутри класса честный узел даёт расхождение ровно 0 —
// замерено на второй карте, другой машине и другом драйвере, 334 текста
// из 334. Прежние 25_000 были запасом к разбросу между картами, которого
// внутри класса не существует.
const CONF_LIMIT: u64 = 0;
/// Две «пробы» по три числа: больше для проверки правила не нужно, а
/// границы текстов обязаны быть именно двумя, иначе максимум по текстам
/// не отличить от среднего по склейке.
const PROBE_LENS: [u32; 2] = [3, 3];
/// Сроки. Те же, что в листах развёртывания: 10 минут на ответ, час на
/// сбор проверок.
const CLAIM_TTL: u64 = 600;
const CHECK_TTL: u64 = 3600;
fn probe_ref() -> Vec<MicroLog> {
    vec![-500_000, -500_000, -500_000, -400_000, -400_000, -400_000]
}
/// Узел, сошедшийся с эталоном. При нулевом пределе это значит ТОЧНО
/// эталон: внутри класса честный узел даёт расхождение ровно 0.
fn probe_honest() -> Vec<MicroLog> {
    probe_ref()
}

/// Узел не того класса: расходится с эталоном, хоть и немного. Шести
/// миллионных достаточно — при нулевом пределе величина не важна.
fn probe_foreign() -> Vec<MicroLog> {
    probe_ref().iter().map(|x| x - TINY as i64).collect()
}

fn setup(deps: DepsMut) {
    instantiate(
        deps,
        mock_env(),
        mock_info(ADMIN, &[]),
        InstantiateMsg {
            admin: ADMIN.into(),
            checks_required: CHECKS,
            pinned_model: MODEL.into(),
            pinned_runtime: RUNTIME.into(),
            pinned_dtype: DTYPE.into(),
            pinned_arch: ARCH.into(),
            kernel_cache_hash: CACHE.into(),
            conformance_limit_micro: CONF_LIMIT,
            conformance_ref_hash: commit_hash(&probe_ref()),
            conformance_lens: PROBE_LENS.to_vec(),
            claim_ttl_seconds: CLAIM_TTL,
            check_ttl_seconds: CHECK_TTL,
            settlement: Settlement::Cw20(Addr::unchecked(CW20)),
            stake_denom: STAKE.into(),
            min_stake: Uint128::new(MIN_STAKE),
            verifier_fee_bps: FEE_BPS,
            forfeit: Forfeit::Burn,
        },
    )
    .unwrap();
}

/// Привести узел в рабочее состояние: регистрация, залог, проба.
///
/// Трёх шагов, а не четырёх: подтверждать допуск подписью больше некому.
/// Регистрация сразу даёт `Active`, а брать задания позволяют залог и
/// проба — ровно то, что проверяет цепочка.
fn register_and_stake(deps: DepsMut, who: &str) {
    let mut d = deps;
    execute(
        d.branch(),
        mock_env(),
        mock_info(who, &[]),
        ExecuteMsg::RegisterExecutor {
            model: MODEL.into(),
            runtime: RUNTIME.into(),
            dtype: DTYPE.into(),
            card_class: ARCH.into(),
            kernel_cache_hash: CACHE.into(),
        },
    )
    .unwrap();
    execute(
        d.branch(),
        mock_env(),
        mock_info(who, &coins(MIN_STAKE, STAKE)),
        ExecuteMsg::DepositStake {},
    )
    .unwrap();
    // Адрес доставки — четвёртый шаг, и без него заданий не дают:
    // связку исполнителя взять было бы негде.
    announce(d.branch(), who, ENDPOINT);
    // Внутри класса честный узел заявляет РОВНО ноль: его вектор
    // тождествен эталонному.
    pass_probe(d.branch(), who, 0);
}

/// Объявить адрес доставки.
fn announce(deps: DepsMut, who: &str, uri: &str) {
    execute(
        deps,
        mock_env(),
        mock_info(who, &[]),
        ExecuteMsg::SetEndpoint {
            endpoint: Some(uri.into()),
        },
    )
    .unwrap();
}

/// Сдать пробу. Число заявляется исполнителем, обязательство — его
/// векторами: контракт порождать ничего не может и проверяет заявленное
/// только при опровержении.
fn pass_probe(deps: DepsMut, who: &str, claim: u64) {
    execute(
        deps,
        mock_env(),
        mock_info(who, &[]),
        ExecuteMsg::SubmitConformance {
            divergence_micro: claim,
            logprobs_hash: commit_hash(&probe_honest()),
        },
    )
    .unwrap();
}

/// Зарегистрировать узел и провести пробу, без залога и допуска: это
/// всё, что нужно проверяющему.
fn register_and_probe(deps: DepsMut, who: &str) {
    let mut d = deps;
    execute(
        d.branch(),
        mock_env(),
        mock_info(who, &[]),
        ExecuteMsg::RegisterExecutor {
            model: MODEL.into(),
            runtime: RUNTIME.into(),
            dtype: DTYPE.into(),
            card_class: ARCH.into(),
            kernel_cache_hash: CACHE.into(),
        },
    )
    .unwrap();
    // Внутри класса честный узел заявляет РОВНО ноль: его вектор
    // тождествен эталонному.
    pass_probe(d.branch(), who, 0);
}

/// Заказчик ставит задание. Расчётная единица — CW20, поэтому вызов
/// приходит ОТ её контракта, а не от заказчика.
fn submit_job(deps: DepsMut) {
    execute(
        deps,
        mock_env(),
        mock_info(CW20, &[]),
        ExecuteMsg::Receive(Cw20ReceiveMsg {
            sender: CLIENT.into(),
            amount: Uint128::new(TOTAL),
            msg: to_json_binary(&ReceiveHook::SubmitJob {
                prompt_hash: "ph".into(), prompt_uri: PROMPT_URI.into(),
                price: Uint128::new(PRICE),
            })
            .unwrap(),
        }),
    )
    .unwrap();
}

/// Доводит задание до состояния Answered и возвращает его номер.
fn job_until_answered(deps: DepsMut) -> u64 {
    let mut d = deps;
    submit_job(d.branch());
    execute(
        d.branch(),
        mock_env(),
        mock_info(EXEC, &[]),
        ExecuteMsg::ClaimJob { job_id: 1 },
    )
    .unwrap();
    execute(
        d.branch(),
        mock_env(),
        mock_info(EXEC, &[]),
        ExecuteMsg::SubmitAnswer {
            job_id: 1,
            answer_hash: "ah".into(),
            logprobs_hash: "lh".into(),
            answer_tokens: 120,
        },
    )
    .unwrap();
    1
}

/// Сдать проверки. `matched[i]` — совпал ли хэш i-го проверяющего с
/// обязательством исполнителя; `evidence[i]` — расхождение, которое он
/// заявляет.
///
/// Вердикт решает ТОЛЬКО совпадение хэша. Расхождение сдаётся как
/// свидетельство: по нему видно, насколько разошлись, и по нему
/// разбирается спор. Поэтому в тестах оно нарочно берётся то крошечным,
/// то огромным — вердикт от него не меняется.
fn submit_checks_with(deps: DepsMut, job_id: u64, matched: &[bool], evidence: &[u64]) {
    let mut d = deps;
    for (i, m) in matched.iter().enumerate() {
        let who = format!("checker{}", i);
        // Проверяющий тоже обязан пройти пробу: сравниваются его числа.
        register_and_probe(d.branch(), &who);
        execute(
            d.branch(),
            mock_env(),
            mock_info(&who, &[]),
            ExecuteMsg::SubmitCheck {
                job_id,
                divergence_micro: evidence[i],
                logprobs_hash: if *m {
                    COMMIT.into()
                } else {
                    format!("другой{}", i)
                },
            },
        )
        .unwrap();
    }
}

/// Все проверки совпали с исполнителем. Расхождение при этом нулевое —
/// внутри класса честные векторы тождественны.
fn submit_checks_matching(deps: DepsMut, job_id: u64, n: usize) {
    submit_checks_with(deps, job_id, &vec![true; n], &vec![0; n]);
}

/// Ни одна не совпала, и расхождение при этом КРОШЕЧНОЕ — шесть
/// миллионных. Настоящее наблюдение: столько даёт разный блок свёртки на
/// Ada, и этого довольно, чтобы равенство сломалось.
fn submit_checks_mismatching(deps: DepsMut, job_id: u64, n: usize) {
    submit_checks_with(deps, job_id, &vec![false; n], &vec![TINY; n]);
}

#[test]
fn chuzhaya_svyazka_otklonyaetsya() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    // Обновлённый рантайм обнуляет порог до перезамера, поэтому внутрь
    // такой исполнитель не попадает вовсе.
    let err = execute(
        deps.as_mut(),
        mock_env(),
        mock_info(EXEC, &[]),
        ExecuteMsg::RegisterExecutor {
            model: MODEL.into(),
            runtime: "vllm-0.32.0".into(),
            dtype: DTYPE.into(),
            card_class: ARCH.into(),
            kernel_cache_hash: CACHE.into(),
        },
    )
    .unwrap_err();
    assert!(matches!(err, ContractError::WrongPinning { .. }));
}

#[test]
fn odnoy_registracii_ne_hvataet() {
    // Прежде этот случай назывался «без допуска»: узел ждал подписи
    // аттестора. Теперь подписи нет, статус сразу Active — и задание он
    // всё равно не возьмёт, потому что цепочка требует залог и пробу.
    // Именно это и есть замена аттестора: условие не исчезло, оно
    // перестало зависеть от человека.
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    execute(
        deps.as_mut(),
        mock_env(),
        mock_info(EXEC, &[]),
        ExecuteMsg::RegisterExecutor {
            model: MODEL.into(),
            runtime: RUNTIME.into(),
            dtype: DTYPE.into(),
            card_class: ARCH.into(),
            kernel_cache_hash: CACHE.into(),
        },
    )
    .unwrap();
    let e: Executor = from_json(
        query(
            deps.as_ref(),
            mock_env(),
            QueryMsg::Executor {
                address: EXEC.into(),
            },
        )
        .unwrap(),
    )
    .unwrap();
    assert_eq!(e.status, ExecutorStatus::Active,
               "регистрация даёт Active: подтверждать некому");

    submit_job(deps.as_mut());
    // Залога нет — и дальше этого не уйти.
    let err = execute(
        deps.as_mut(),
        mock_env(),
        mock_info(EXEC, &[]),
        ExecuteMsg::ClaimJob { job_id: 1 },
    )
    .unwrap_err();
    assert!(matches!(err, ContractError::StakeTooSmall { .. }),
            "вышло {err:?}");
    // Залог внесён, но пробы нет — всё равно нет.
    execute(
        deps.as_mut(),
        mock_env(),
        mock_info(EXEC, &coins(MIN_STAKE, STAKE)),
        ExecuteMsg::DepositStake {},
    )
    .unwrap();
    let err = execute(
        deps.as_mut(),
        mock_env(),
        mock_info(EXEC, &[]),
        ExecuteMsg::ClaimJob { job_id: 1 },
    )
    .unwrap_err();
    assert!(matches!(err, ContractError::ConformanceMissing),
            "вышло {err:?}");
    // Проба есть, а адреса доставки нет — и это тоже не допуск: связку
    // такого узла взять негде, проверять его ответ будет нечем.
    pass_probe(deps.as_mut(), EXEC, 0);
    let err = execute(
        deps.as_mut(),
        mock_env(),
        mock_info(EXEC, &[]),
        ExecuteMsg::ClaimJob { job_id: 1 },
    )
    .unwrap_err();
    assert!(matches!(err, ContractError::NoEndpoint), "вышло {err:?}");
    // И только со всем вместе — да.
    announce(deps.as_mut(), EXEC, ENDPOINT);
    execute(
        deps.as_mut(),
        mock_env(),
        mock_info(EXEC, &[]),
        ExecuteMsg::ClaimJob { job_id: 1 },
    )
    .unwrap();
}

#[test]
fn korotkiy_otvet_bolshe_ne_otklonyaetsya() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    submit_job(deps.as_mut());
    execute(
        deps.as_mut(),
        mock_env(),
        mock_info(EXEC, &[]),
        ExecuteMsg::ClaimJob { job_id: 1 },
    )
    .unwrap();
    // Прежде 31 токен отклонялся: на коротких ответах запас порога падал
    // до 1,6x. Порога больше нет, а равенство хэшей от длины не зависит —
    // и замерено прямо, что отсечка по длине ответа пересечения полос не
    // убирала ни при 32 токенах, ни при 128. Значит гейт был лишним, и
    // короткий ответ принимается.
    execute(
        deps.as_mut(),
        mock_env(),
        mock_info(EXEC, &[]),
        ExecuteMsg::SubmitAnswer {
            job_id: 1,
            answer_hash: "ah".into(),
            logprobs_hash: COMMIT.into(),
            answer_tokens: 31,
        },
    )
    .unwrap();
    let job: Job = from_json(
        query(deps.as_ref(), mock_env(), QueryMsg::Job { job_id: 1 }).unwrap(),
    )
    .unwrap();
    assert_eq!(job.state, JobState::Answered);
    assert_eq!(job.answer_tokens, Some(31));
}

#[test]
fn ispolnitel_ne_proveryaet_sebya() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    let err = execute(
        deps.as_mut(),
        mock_env(),
        mock_info(EXEC, &[]),
        ExecuteMsg::SubmitCheck {
            job_id: id,
            divergence_micro: 100,
            logprobs_hash: "h".into(),
        },
    )
    .unwrap_err();
    assert!(matches!(err, ContractError::SelfCheck));
}

#[test]
fn odin_proveryayushchiy_ne_sdast_dvazhdy() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    submit_checks_matching(deps.as_mut(), id, 1);
    let err = execute(
        deps.as_mut(),
        mock_env(),
        mock_info("checker0", &[]),
        ExecuteMsg::SubmitCheck {
            job_id: id,
            divergence_micro: HONEST_MEAN,
            logprobs_hash: "h".into(),
        },
    )
    .unwrap_err();
    assert!(matches!(err, ContractError::DuplicateCheck));
}

#[test]
fn nepolnoe_okno_ne_svoditsya() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    submit_checks_mismatching(deps.as_mut(), id, 1);
    let err = execute(
        deps.as_mut(),
        mock_env(),
        mock_info("anyone", &[]),
        ExecuteMsg::Finalize { job_id: id },
    )
    .unwrap_err();
    assert!(matches!(
        err,
        ContractError::NotEnoughChecks { got: 1, need: 2 }
    ));
}

#[test]
fn podlog_na_samom_nezametnom_lovitsya() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    // Величина расхождения вердикта НЕ решает, и это главная перемена.
    // Прежде здесь брался минимум измеренного подлога — 37 780, едва над
    // порогом 36 000. Теперь берётся шесть миллионных: столько даёт
    // разный блок свёртки на Ada, и этого довольно, чтобы хэш разошёлся.
    submit_checks_with(deps.as_mut(), id, &[false, false], &[TINY, TINY]);
    let res = execute(
        deps.as_mut(),
        mock_env(),
        mock_info("anyone", &[]),
        ExecuteMsg::Finalize { job_id: id },
    )
    .unwrap();
    assert!(res
        .attributes
        .iter()
        .any(|a| a.key == "guilty" && a.value == "true"));

    let job: Job = from_json(
        query(deps.as_ref(), mock_env(), QueryMsg::Job { job_id: id }).unwrap(),
    )
    .unwrap();
    assert_eq!(job.guilty, Some(true));
    assert_eq!(job.state, JobState::Settled);

    let e: Executor = from_json(
        query(
            deps.as_ref(),
            mock_env(),
            QueryMsg::Executor {
                address: EXEC.into(),
            },
        )
        .unwrap(),
    )
    .unwrap();
    assert_eq!(e.status, ExecutorStatus::Banned);
    assert_eq!(e.convictions, 1);
}

#[test]
fn tri_iz_chetyreh_eto_ne_vina() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    // Одна проверка не совпала, другая совпала — правило «не совпало у
    // всех» не срабатывает. Это и есть защита от лгущего проверяющего:
    // чтобы обвинить честного, сговориться должны ВСЕ.
    //
    // Расхождение у несовпавшей взято огромным — 143 310, настоящий
    // подлог FP8. Вердикт от этого не меняется: решает совпадение хэша.
    submit_checks_with(deps.as_mut(), id, &[false, true], &[HUGE, 0]);
    execute(
        deps.as_mut(),
        mock_env(),
        mock_info("anyone", &[]),
        ExecuteMsg::Finalize { job_id: id },
    )
    .unwrap();
    let job: Job = from_json(
        query(deps.as_ref(), mock_env(), QueryMsg::Job { job_id: id }).unwrap(),
    )
    .unwrap();
    assert_eq!(job.guilty, Some(false));
    let e: Executor = from_json(
        query(
            deps.as_ref(),
            mock_env(),
            QueryMsg::Executor {
                address: EXEC.into(),
            },
        )
        .unwrap(),
    )
    .unwrap();
    assert_eq!(e.status, ExecutorStatus::Active);
    assert_eq!(e.jobs_done, 1);
}

#[test]
fn hudshee_chestnoe_nablyudenie_ne_obvinyaet() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    // Проверяющие заявляют БОЛЬШИЕ расхождения — 12 700, честное среднее
    // из прежних замеров, и это заведомо выше отменённого порога 36 000
    // не дотягивает, а вот 143 310 дотягивает с лихвой. И всё равно
    // обвинения нет: хэш у обоих совпал с исполнителем.
    //
    // Прежде этот тест держался на том, что порог превышен не всеми.
    // Теперь — на том, что порог не при чём вовсе.
    submit_checks_with(deps.as_mut(), id, &[true, true], &[HONEST_MEAN, HUGE]);
    execute(
        deps.as_mut(),
        mock_env(),
        mock_info("anyone", &[]),
        ExecuteMsg::Finalize { job_id: id },
    )
    .unwrap();
    let job: Job = from_json(
        query(deps.as_ref(), mock_env(), QueryMsg::Job { job_id: id }).unwrap(),
    )
    .unwrap();
    assert_eq!(job.guilty, Some(false));
}

#[test]
fn nulevoe_rashozhdenie_pri_chuzhom_heshe_tozhe_vina() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    // Крайний случай с другой стороны: проверяющие заявляют расхождение
    // НОЛЬ, а хэш сдают чужой. Это противоречие — ноль означает
    // тождественные векторы, а тождественные векторы дают тот же хэш, —
    // и вердикт идёт по хэшу. Разберёт противоречие спор: пересчёт на
    // цепочке покажет, что заявленный ноль не сходится с векторами.
    submit_checks_with(deps.as_mut(), id, &[false, false], &[0, 0]);
    execute(
        deps.as_mut(),
        mock_env(),
        mock_info("anyone", &[]),
        ExecuteMsg::Finalize { job_id: id },
    )
    .unwrap();
    let job: Job = from_json(
        query(deps.as_ref(), mock_env(), QueryMsg::Job { job_id: id }).unwrap(),
    )
    .unwrap();
    assert_eq!(job.guilty, Some(true));
}

#[test]
fn pyataya_proverka_ne_prinimaetsya() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    submit_checks_matching(deps.as_mut(), id, CHECKS as usize);
    // Лишние проверки меняли бы долю ложных обвинений, поэтому окно
    // закрывается ровно на checks_required.
    register_and_probe(deps.as_mut(), "checker9");
    let err = execute(
        deps.as_mut(),
        mock_env(),
        mock_info("checker9", &[]),
        ExecuteMsg::SubmitCheck {
            job_id: id,
            divergence_micro: HONEST_MEAN,
            logprobs_hash: "h".into(),
        },
    )
    .unwrap_err();
    assert!(matches!(err, ContractError::ChecksComplete));
}

#[test]
fn zabanennogo_nelzya_vernut() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    submit_checks_mismatching(deps.as_mut(), id, CHECKS as usize);
    execute(
        deps.as_mut(),
        mock_env(),
        mock_info("anyone", &[]),
        ExecuteMsg::Finalize { job_id: id },
    )
    .unwrap();
    // Вернуть забаненного не может НИКТО, и теперь это проверяется иначе,
    // чем прежде: сообщений, меняющих статус исполнителя, в контракте не
    // осталось вовсе. Сперва это был `Attest` аттестора, потом
    // `SetSuspended` админа — убраны оба. Единственное, что меняет статус,
    // — вердикт цепочки и уровень залога.
    let e: Executor = from_json(query(deps.as_ref(), mock_env(),
        QueryMsg::Executor { address: EXEC.into() }).unwrap()).unwrap();
    assert_eq!(e.status, ExecutorStatus::Banned);
    // Донести залог забаненному можно — и это его не возвращает.
    let err = execute(
        deps.as_mut(),
        mock_env(),
        mock_info(EXEC, &coins(MIN_STAKE, STAKE)),
        ExecuteMsg::DepositStake {},
    )
    .unwrap_err();
    assert!(matches!(err, ContractError::ExecutorBanned), "вышло {err:?}");
    let e: Executor = from_json(query(deps.as_ref(), mock_env(),
        QueryMsg::Executor { address: EXEC.into() }).unwrap()).unwrap();
    assert_eq!(e.status, ExecutorStatus::Banned, "бан не снялся");
}

#[test]
fn prava_razdeleny() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    // Настройки — только админ. Посторонний не меняет ничего.
    let err = execute(
        deps.as_mut(),
        mock_env(),
        mock_info("postoronniy", &[]),
        ExecuteMsg::UpdateConfig {
            admin: None,
            checks_required: Some(1),
            pinned_model: None,
            pinned_runtime: None,
            pinned_dtype: None,
            pinned_arch: None,
            kernel_cache_hash: None,
            conformance_limit_micro: None,
            conformance_ref: None,
            claim_ttl_seconds: None,
            check_ttl_seconds: None,
            settlement: None,
            stake_denom: None,
            min_stake: None,
            verifier_fee_bps: None,
            forfeit: None,
        },
    )
    .unwrap_err();
    assert!(matches!(err, ContractError::NotAdmin));
    // А допуск не подтверждает НИКТО, админ в том числе: сообщения для
    // этого в контракте больше нет. Узел становится Active сам, одной
    // регистрацией, и дальше всё решают залог и проба.
    execute(
        deps.as_mut(),
        mock_env(),
        mock_info(EXEC, &[]),
        ExecuteMsg::RegisterExecutor {
            model: MODEL.into(),
            runtime: RUNTIME.into(),
            dtype: DTYPE.into(),
            card_class: ARCH.into(),
            kernel_cache_hash: CACHE.into(),
        },
    )
    .unwrap();
    let e: Executor = from_json(query(deps.as_ref(), mock_env(),
        QueryMsg::Executor { address: EXEC.into() }).unwrap()).unwrap();
    assert_eq!(e.status, ExecutorStatus::Active,
               "регистрация даёт Active без чьей-либо подписи");
    // И всё же задание ему не достанется: ни залога, ни пробы.
    submit_job(deps.as_mut());
    let err = execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::ClaimJob { job_id: 1 }).unwrap_err();
    assert!(matches!(err, ContractError::StakeTooSmall { .. }),
            "вышло {err:?}");
}

#[test]
fn nol_proverok_zapreshchen() {
    let mut deps = mock_dependencies();
    let err = instantiate(
        deps.as_mut(),
        mock_env(),
        mock_info(ADMIN, &[]),
        InstantiateMsg {
            admin: ADMIN.into(),
            checks_required: 0,
            pinned_model: MODEL.into(),
            pinned_runtime: RUNTIME.into(),
            pinned_dtype: DTYPE.into(),
            pinned_arch: ARCH.into(),
            kernel_cache_hash: CACHE.into(),
            conformance_limit_micro: CONF_LIMIT,
            conformance_ref_hash: commit_hash(&probe_ref()),
            conformance_lens: PROBE_LENS.to_vec(),
            claim_ttl_seconds: CLAIM_TTL,
            check_ttl_seconds: CHECK_TTL,
            settlement: Settlement::Cw20(Addr::unchecked(CW20)),
            stake_denom: STAKE.into(),
            min_stake: Uint128::new(MIN_STAKE),
            verifier_fee_bps: FEE_BPS,
            forfeit: Forfeit::Burn,
        },
    )
    .unwrap_err();
    assert!(matches!(err, ContractError::ZeroChecks));
}

#[test]
fn zapros_proverok_schitaet_nesovpadeniya() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    // Одна совпала, одна нет — запрос обязан считать НЕСОВПАДЕНИЯ, а не
    // превышения порога: порога больше нет.
    submit_checks_with(deps.as_mut(), id, &[false, true], &[HUGE, 0]);
    let r: ChecksResponse = from_json(
        query(deps.as_ref(), mock_env(), QueryMsg::Checks { job_id: id }).unwrap(),
    )
    .unwrap();
    assert_eq!(r.checks.len(), 2);
    assert_eq!(r.mismatched, 1);
    let _ = Addr::unchecked(EXEC);
}

// --- шаг 9: спор по проверке ---------------------------------------------
//
// Векторы взяты правдоподобными: logprob всегда отрицателен, у честной
// пары элементы близки, у подлога разъезжаются. Числа в микродолях.

use crate::contract::divergence_micro;

/// Доводит задание до Answered с ОБЯЗАТЕЛЬСТВОМ по настоящему вектору.
fn job_with_commit(deps: DepsMut, producer: &[i64]) -> u64 {
    let mut d = deps;
    submit_job(d.branch());
    execute(d.branch(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::ClaimJob { job_id: 1 }).unwrap();
    execute(d.branch(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::SubmitAnswer {
            job_id: 1,
            answer_hash: "ah".into(),
            logprobs_hash: commit_hash(producer),
            answer_tokens: 120,
        }).unwrap();
    1
}

#[test]
fn hesh_odnoznachen_i_zavisit_ot_poryadka() {
    // Канон обязан давать одно представление: иначе два честных узла
    // получат разные хэши от одного вектора.
    assert_eq!(commit_hash(&[-693147, -120000]), commit_hash(&[-693147, -120000]));
    assert_ne!(commit_hash(&[-693147, -120000]), commit_hash(&[-120000, -693147]));
    assert_ne!(commit_hash(&[-693147]), commit_hash(&[-693147, 0]));
    assert_eq!(commit_hash(&[-1]).len(), 64);
}

#[test]
fn pereschyot_rashozhdeniya_sovpadaet_s_zamerom() {
    // Честная пара: элементы отличаются примерно на честное среднее.
    let a: Vec<i64> = (0..100).map(|i| -500_000 - i * 1000).collect();
    let b: Vec<i64> = a.iter().map(|x| x - 12_700).collect();
    assert_eq!(divergence_micro(&a, &b).unwrap(), 12_700);
    // Разная длина считается по общей части, а не отвергается.
    assert_eq!(divergence_micro(&a, &b[..50]).unwrap(), 12_700);
    // Пустой вектор — отказ, а не ноль: ноль выглядел бы как согласие.
    assert!(matches!(divergence_micro(&[], &b), Err(ContractError::EmptyVector)));
}

#[test]
fn chestnyy_proveryayushchiy_podtverzhdaetsya() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let prod: Vec<i64> = (0..100).map(|i| -500_000 - i * 1000).collect();
    let chk: Vec<i64> = prod.iter().map(|x| x - 12_700).collect();
    let id = job_with_commit(deps.as_mut(), &prod);
    register_and_probe(deps.as_mut(), "checker0");
    execute(deps.as_mut(), mock_env(), mock_info("checker0", &[]),
        ExecuteMsg::SubmitCheck {
            job_id: id,
            divergence_micro: 12_700,
            logprobs_hash: commit_hash(&chk),
        }).unwrap();
    let res = execute(deps.as_mut(), mock_env(), mock_info("anyone", &[]),
        ExecuteMsg::ResolveDispute {
            job_id: id,
            checker: "checker0".into(),
            producer_logprobs: prod,
            checker_logprobs: chk,
        }).unwrap();
    assert!(res.attributes.iter().any(|a| a.key == "honest" && a.value == "true"));
}

#[test]
fn vrushchiy_proveryayushchiy_lovitsya() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let prod: Vec<i64> = (0..100).map(|i| -500_000 - i * 1000).collect();
    let chk: Vec<i64> = prod.iter().map(|x| x - 12_700).collect();
    let id = job_with_commit(deps.as_mut(), &prod);
    // Заявил подлог там, где его нет: обязался честным вектором, а число
    // назвал выше порога. Это и есть ложное обвинение.
    register_and_probe(deps.as_mut(), "checker0");
    execute(deps.as_mut(), mock_env(), mock_info("checker0", &[]),
        ExecuteMsg::SubmitCheck {
            job_id: id,
            divergence_micro: HUGE,
            logprobs_hash: commit_hash(&chk),
        }).unwrap();
    let res = execute(deps.as_mut(), mock_env(), mock_info("anyone", &[]),
        ExecuteMsg::ResolveDispute {
            job_id: id,
            checker: "checker0".into(),
            producer_logprobs: prod,
            checker_logprobs: chk,
        }).unwrap();
    assert!(res.attributes.iter().any(|a| a.key == "honest" && a.value == "false"));
    assert!(res.attributes.iter().any(|a| a.key == "recomputed" && a.value == "12700"));
}

#[test]
fn podmenennyy_vektor_otvergaetsya() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let prod: Vec<i64> = (0..100).map(|i| -500_000 - i * 1000).collect();
    let chk: Vec<i64> = prod.iter().map(|x| x - 12_700).collect();
    let id = job_with_commit(deps.as_mut(), &prod);
    register_and_probe(deps.as_mut(), "checker0");
    execute(deps.as_mut(), mock_env(), mock_info("checker0", &[]),
        ExecuteMsg::SubmitCheck {
            job_id: id,
            divergence_micro: 12_700,
            logprobs_hash: commit_hash(&chk),
        }).unwrap();
    // Исполнитель пытается подсунуть другой вектор — тот, что выгоден
    // ему после того, как он увидел результат проверки. Ровно от этого
    // обязательство хэшем и защищает.
    let mut podmena = prod.clone();
    podmena[0] += 50_000;
    let err = execute(deps.as_mut(), mock_env(), mock_info("anyone", &[]),
        ExecuteMsg::ResolveDispute {
            job_id: id,
            checker: "checker0".into(),
            producer_logprobs: podmena,
            checker_logprobs: chk,
        }).unwrap_err();
    assert!(matches!(err, ContractError::RevealMismatch { .. }));
}

#[test]
fn posle_verdikta_sporit_pozdno() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let prod: Vec<i64> = (0..100).map(|i| -500_000 - i * 1000).collect();
    let chk: Vec<i64> = prod.iter().map(|x| x - 12_700).collect();
    let id = job_with_commit(deps.as_mut(), &prod);
    for i in 0..CHECKS {
        register_and_probe(deps.as_mut(), &format!("checker{}", i));
        execute(deps.as_mut(), mock_env(), mock_info(&format!("checker{}", i), &[]),
            ExecuteMsg::SubmitCheck {
                job_id: id,
                divergence_micro: HONEST_MEAN,
                logprobs_hash: commit_hash(&chk),
            }).unwrap();
    }
    execute(deps.as_mut(), mock_env(), mock_info("anyone", &[]),
        ExecuteMsg::Finalize { job_id: id }).unwrap();
    let err = execute(deps.as_mut(), mock_env(), mock_info("anyone", &[]),
        ExecuteMsg::ResolveDispute {
            job_id: id,
            checker: "checker0".into(),
            producer_logprobs: prod,
            checker_logprobs: chk,
        }).unwrap_err();
    assert!(matches!(err, ContractError::AlreadySettled));
}

// --- шаг 14: деньги ------------------------------------------------------

#[test]
fn bez_zaloga_zadanie_ne_vzyat() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    // Зарегистрировался, но залога не внёс. Проверка залога стоит при
    // ВЗЯТИИ задания, а не только при регистрации: иначе залог можно
    // забрать сразу после допуска и работать без него.
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::RegisterExecutor {
            model: MODEL.into(), runtime: RUNTIME.into(),
            dtype: DTYPE.into(), card_class: ARCH.into(),
            kernel_cache_hash: CACHE.into(),
        }).unwrap();
    submit_job(deps.as_mut());
    let err = execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::ClaimJob { job_id: 1 }).unwrap_err();
    assert!(matches!(err, ContractError::StakeTooSmall { .. }));
}

#[test]
fn chuzhoy_cw20_ne_oplachivaet() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    // Иначе любой объявил бы задание оплаченным своим токеном.
    let err = execute(deps.as_mut(), mock_env(), mock_info("fake_token", &[]),
        ExecuteMsg::Receive(Cw20ReceiveMsg {
            sender: CLIENT.into(),
            amount: Uint128::new(TOTAL),
            msg: to_json_binary(&ReceiveHook::SubmitJob {
                prompt_hash: "ph".into(), prompt_uri: PROMPT_URI.into(),
                price: Uint128::new(PRICE) }).unwrap(),
        })).unwrap_err();
    assert!(matches!(err, ContractError::WrongCw20 { .. }));
}

#[test]
fn zalog_derzhitsya_nezakrytym_zadaniem() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let _ = job_until_answered(deps.as_mut());
    // Иначе залог уходит ровно перед вердиктом.
    let err = execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::WithdrawStake { amount: Uint128::new(1) }).unwrap_err();
    assert!(matches!(err, ContractError::StakeLocked { open: 1 }));
}

#[test]
fn chestnomu_platyat_a_proveryayushchim_dolyu() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    submit_checks_matching(deps.as_mut(), id, CHECKS as usize);
    let res = execute(deps.as_mut(), mock_env(), mock_info("anyone", &[]),
        ExecuteMsg::Finalize { job_id: id }).unwrap();
    // 970 bps от 1 000 000 это 97 000, на четверых по 24 250 ровно.
    // Исполнителю достаётся ЦЕНА ЦЕЛИКОМ: долю внёс заказчик сверху.
    assert!(res.attributes.iter().any(|a| a.key == "verifier_fees"
        && a.value == FEE.to_string()));
    assert!(res.attributes.iter().any(|a| a.key == "settled"
        && a.value == PRICE.to_string()));
    // Четыре сообщения проверяющим плюс одно исполнителю.
    assert_eq!(res.messages.len(), 3);
}

#[test]
fn vinovnomu_ne_platyat_zalog_izymayut() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    submit_checks_mismatching(deps.as_mut(), id, CHECKS as usize);
    let res = execute(deps.as_mut(), mock_env(), mock_info("anyone", &[]),
        ExecuteMsg::Finalize { job_id: id }).unwrap();
    assert!(res.attributes.iter().any(|a| a.key == "guilty" && a.value == "true"));
    // Четыре проверяющим, возврат заказчику, изъятие залога.
    assert_eq!(res.messages.len(), 4);
    let e: Executor = from_json(query(deps.as_ref(), mock_env(),
        QueryMsg::Executor { address: EXEC.into() }).unwrap()).unwrap();
    assert_eq!(e.stake, Uint128::zero(), "залог изъят");
    assert_eq!(e.status, ExecutorStatus::Banned);
}

#[test]
fn proveryayushchim_platyat_nezavisimo_ot_verdikta() {
    // Плати им из исхода — появится интерес к исходу. Доля обязана быть
    // одинаковой при обоих вердиктах.
    let mut fees = vec![];
    for matched in [true, false] {
        let mut deps = mock_dependencies();
        setup(deps.as_mut());
        register_and_stake(deps.as_mut(), EXEC);
        let id = job_until_answered(deps.as_mut());
        if matched {
            submit_checks_matching(deps.as_mut(), id, CHECKS as usize);
        } else {
            submit_checks_mismatching(deps.as_mut(), id, CHECKS as usize);
        }
        let res = execute(deps.as_mut(), mock_env(), mock_info("anyone", &[]),
            ExecuteMsg::Finalize { job_id: id }).unwrap();
        fees.push(res.attributes.iter()
            .find(|a| a.key == "verifier_fees").unwrap().value.clone());
    }
    assert_eq!(fees[0], fees[1], "плата проверяющим не зависит от вердикта");
}

#[test]
fn konfiskat_uhodit_po_adresu_a_ne_obvinyavshim() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    execute(deps.as_mut(), mock_env(), mock_info(ADMIN, &[]),
        ExecuteMsg::UpdateConfig {
            admin: None, checks_required: None,
            pinned_arch: None, kernel_cache_hash: None,
            pinned_model: None, pinned_runtime: None,
            pinned_dtype: None, conformance_limit_micro: None, conformance_ref: None,
            claim_ttl_seconds: None, check_ttl_seconds: None,
            settlement: None, stake_denom: None,
            min_stake: None, verifier_fee_bps: None,
            forfeit: Some(Forfeit::ToAddress(Addr::unchecked("community_pool"))),
        }).unwrap();
    register_and_stake(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    submit_checks_mismatching(deps.as_mut(), id, CHECKS as usize);
    let res = execute(deps.as_mut(), mock_env(), mock_info("anyone", &[]),
        ExecuteMsg::Finalize { job_id: id }).unwrap();
    let dump = format!("{:?}", res.messages);
    assert!(dump.contains("community_pool"), "конфискат ушёл по адресу");
    // Залог в uaxm, а проверяющим платят в CW20 — конфискат к ним попасть
    // не может по построению, и варианта ToVerifiers в типе нет вовсе.
    assert!(dump.contains("uaxm"), "изъятие в денoме залога");
}

#[test]
fn dvazhdy_ne_vyplachivaetsya() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    submit_checks_matching(deps.as_mut(), id, CHECKS as usize);
    execute(deps.as_mut(), mock_env(), mock_info("anyone", &[]),
        ExecuteMsg::Finalize { job_id: id }).unwrap();
    let err = execute(deps.as_mut(), mock_env(), mock_info("anyone", &[]),
        ExecuteMsg::Finalize { job_id: id }).unwrap_err();
    assert!(matches!(err, ContractError::WrongJobState { .. }));
}

#[test]
fn dolya_svyshe_sta_procentov_zapreshchena() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    let err = execute(deps.as_mut(), mock_env(), mock_info(ADMIN, &[]),
        ExecuteMsg::UpdateConfig {
            admin: None, checks_required: None,
            pinned_arch: None, kernel_cache_hash: None,
            pinned_model: None, pinned_runtime: None,
            pinned_dtype: None, conformance_limit_micro: None, conformance_ref: None,
            claim_ttl_seconds: None, check_ttl_seconds: None,
            settlement: None, stake_denom: None,
            min_stake: None, verifier_fee_bps: Some(10_001), forfeit: None,
        }).unwrap_err();
    assert!(matches!(err, ContractError::BadFeeBps { bps: 10_001 }));
}

#[test]
fn zabanennyy_zalog_ne_zaberyot() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    submit_checks_mismatching(deps.as_mut(), id, CHECKS as usize);
    execute(deps.as_mut(), mock_env(), mock_info("anyone", &[]),
        ExecuteMsg::Finalize { job_id: id }).unwrap();
    // Забанить и дать уйти с залогом — значит не наказать вовсе.
    let err = execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::WithdrawStake { amount: Uint128::new(1) }).unwrap_err();
    assert!(matches!(err, ContractError::ExecutorBanned));
}

// --- проба на эталонном векторе ------------------------------------------
//
// Зачем она вообще: прибитая тройка модель/версия/dtype проверяется по
// ИМЕНАМ, а замер показал, что имён недостаточно. На одной карте, с той же
// моделью, версией и dtype, --enforce-eager против пути по умолчанию дал
// ни одного побитового совпадения из 40 и расхождение до 413 488 — внутри
// полосы подлога. Честный узел в неверном режиме выглядит жуликом, и
// заявленная тройка у него верная.

#[test]
fn bez_proby_zadanie_ne_vzyat() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    // Зарегистрирован, залог внесён — всё, кроме пробы.
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::RegisterExecutor {
            model: MODEL.into(), runtime: RUNTIME.into(),
            dtype: DTYPE.into(), card_class: ARCH.into(),
            kernel_cache_hash: CACHE.into(),
        }).unwrap();
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &coins(MIN_STAKE, STAKE)),
        ExecuteMsg::DepositStake {}).unwrap();
    submit_job(deps.as_mut());
    let err = execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::ClaimJob { job_id: 1 }).unwrap_err();
    assert!(matches!(err, ContractError::ConformanceMissing));
}

#[test]
fn proba_vyshe_predela_ne_prinimaetsya() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::RegisterExecutor {
            model: MODEL.into(), runtime: RUNTIME.into(),
            dtype: DTYPE.into(), card_class: ARCH.into(),
            kernel_cache_hash: CACHE.into(),
        }).unwrap();
    let err = execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::SubmitConformance {
            divergence_micro: CONF_LIMIT + 1,
            logprobs_hash: commit_hash(&probe_honest()),
        }).unwrap_err();
    assert!(matches!(err, ContractError::ConformanceFailed { got, limit }
        if got == CONF_LIMIT + 1 && limit == CONF_LIMIT));
}

#[test]
fn chuzhaya_arhitektura_v_klass_ne_vhodit() {
    // Прежде здесь утверждалось обратное: «требовать одинаковых карт
    // было бы неверно, честный узел на A100 расходится с 4090 максимум
    // на 20 137, ниже предела». Это было верно для порога и неверно
    // теперь: вердикт выносится РАВЕНСТВОМ, а между архитектурами
    // равенства не бывает вовсе — те же 334 текста на Ampere и Ada не
    // совпали ни на одном, среднее расхождение 15 178.
    //
    // Поэтому чужая архитектура отсеивается на регистрации, и это не
    // обвинение: такой узел НЕСРАВНИМ, а не жульничает.
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    let err = execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::RegisterExecutor {
            model: MODEL.into(), runtime: RUNTIME.into(),
            dtype: DTYPE.into(), card_class: "sm_80".into(),
            kernel_cache_hash: CACHE.into(),
        }).unwrap_err();
    assert!(matches!(err, ContractError::WrongClass { .. }), "{err:?}");
}

#[test]
fn chuzhoy_kesh_yader_v_klass_ne_vhodit() {
    // Кеш ядер — такая же часть класса, как архитектура. Из четырёх
    // RTX 3090 три совпали побитово, четвёртая не совпала ни с одной, и
    // различался у неё выбор блока свёртки: R0_BLOCK 2048 против 4096.
    // Кеш совпавшей, перенесённый на неё, дал 300 совпадений из 300.
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    let err = execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::RegisterExecutor {
            model: MODEL.into(), runtime: RUNTIME.into(),
            dtype: DTYPE.into(), card_class: ARCH.into(),
            kernel_cache_hash: "своя сборка".into(),
        }).unwrap_err();
    assert!(matches!(err, ContractError::WrongClass { .. }), "{err:?}");
}

#[test]
fn uzel_svoego_klassa_probu_prohodit_s_nulyom() {
    // Внутри класса честный узел даёт расхождение РОВНО 0: замерено на
    // второй карте, другой машине и другом драйвере — 34 из 34 текстов
    // пробы и 300 из 300 текстов корпуса.
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::RegisterExecutor {
            model: MODEL.into(), runtime: RUNTIME.into(),
            dtype: DTYPE.into(), card_class: ARCH.into(),
            kernel_cache_hash: CACHE.into(),
        }).unwrap();
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::SubmitConformance {
            divergence_micro: 0,
            logprobs_hash: commit_hash(&probe_honest()),
        }).unwrap();
    let e: Executor = from_json(query(deps.as_ref(), mock_env(),
        QueryMsg::Executor { address: EXEC.into() }).unwrap()).unwrap();
    assert_eq!(e.conformance.unwrap().divergence_micro, 0);
}

#[test]
fn smena_svyazki_obnulyaet_probu() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    submit_job(deps.as_mut());
    // Порог измерен для связки; сменили связку — все пробы о прежней.
    execute(deps.as_mut(), mock_env(), mock_info(ADMIN, &[]),
        ExecuteMsg::UpdateConfig {
            admin: None, checks_required: None,
            pinned_arch: None, kernel_cache_hash: None, pinned_model: None,
            pinned_runtime: Some("vllm-0.32.0".into()), pinned_dtype: None,
            conformance_limit_micro: None, conformance_ref: None,
            claim_ttl_seconds: None, check_ttl_seconds: None,
            settlement: None, stake_denom: None, min_stake: None,
            verifier_fee_bps: None, forfeit: None,
        }).unwrap();
    let err = execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::ClaimJob { job_id: 1 }).unwrap_err();
    assert!(matches!(err, ContractError::ConformanceStale { have: 1, want: 2 }));
}

#[test]
fn smena_etalona_obnulyaet_probu() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    submit_job(deps.as_mut());
    execute(deps.as_mut(), mock_env(), mock_info(ADMIN, &[]),
        ExecuteMsg::UpdateConfig {
            admin: None, checks_required: None,
            pinned_arch: None, kernel_cache_hash: None,
            pinned_model: None, pinned_runtime: None,
            pinned_dtype: None, conformance_limit_micro: None,
            conformance_ref: Some(ConformanceRef {
                ref_hash: commit_hash(&probe_foreign()),
                lens: PROBE_LENS.to_vec(),
            }),
            claim_ttl_seconds: None, check_ttl_seconds: None,
            settlement: None, stake_denom: None, min_stake: None,
            verifier_fee_bps: None, forfeit: None,
        }).unwrap();
    let err = execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::ClaimJob { job_id: 1 }).unwrap_err();
    assert!(matches!(err, ContractError::ConformanceStale { have: 1, want: 2 }));
}

#[test]
fn zapis_toy_zhe_svyazki_probu_ne_obnulyaet() {
    // Иначе любая правка настроек — хоть доли проверяющих — выбивала бы
    // из работы всю сеть до повторного прохождения пробы.
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    submit_job(deps.as_mut());
    execute(deps.as_mut(), mock_env(), mock_info(ADMIN, &[]),
        ExecuteMsg::UpdateConfig {
            admin: None, checks_required: None,
            pinned_arch: None, kernel_cache_hash: None, pinned_model: Some(MODEL.into()),
            pinned_runtime: Some(RUNTIME.into()), pinned_dtype: Some(DTYPE.into()),
            conformance_limit_micro: Some(CONF_LIMIT),
            conformance_ref: Some(ConformanceRef {
                ref_hash: commit_hash(&probe_ref()),
                lens: PROBE_LENS.to_vec(),
            }),
            claim_ttl_seconds: None, check_ttl_seconds: None,
            settlement: None, stake_denom: None, min_stake: None,
            verifier_fee_bps: Some(500), forfeit: None,
        }).unwrap();
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::ClaimJob { job_id: 1 }).unwrap();
}

#[test]
fn lozh_v_probe_oproverzhima() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::RegisterExecutor {
            model: MODEL.into(), runtime: RUNTIME.into(),
            dtype: DTYPE.into(), card_class: ARCH.into(),
            kernel_cache_hash: CACHE.into(),
        }).unwrap();
    // Расхождение своих векторов с эталоном он считает сам, поэтому
    // назвать может любое. Обязательство векторами делает это проверяемым.
    //
    // При нулевом пределе принимается только заявление НОЛЬ — значит и
    // лгать приходится нулём, обязуясь векторами, которые расходятся на
    // 100 000. Опровержение это и вскрывает.
    let mine: Vec<MicroLog> = probe_ref().iter().map(|x| x - 100_000).collect();
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::SubmitConformance {
            divergence_micro: 0,
            logprobs_hash: commit_hash(&mine),
        }).unwrap();
    let res = execute(deps.as_mut(), mock_env(), mock_info("anyone", &[]),
        ExecuteMsg::DisputeConformance {
            executor: EXEC.into(),
            reference_logprobs: probe_ref(),
            executor_logprobs: mine,
        }).unwrap();
    assert!(res.attributes.iter().any(|a| a.key == "lied" && a.value == "true"));
    assert!(res.attributes.iter().any(|a| a.key == "actual_micro"
        && a.value == "100000"));
    let e: Executor = from_json(query(deps.as_ref(), mock_env(),
        QueryMsg::Executor { address: EXEC.into() }).unwrap()).unwrap();
    assert_eq!(e.status, ExecutorStatus::Banned);
    assert!(e.conformance.is_none(), "проба снята");
}

#[test]
fn lyuboe_nenulevoe_rashozhdenie_probu_valit() {
    // Прежде этот тест держался на выборе статистики: неверный режим
    // портит РЕДКИЕ тексты (у eager 29 из 40 совпадали побитово), и
    // среднее по склейке такую порчу размывало — зазор падал с 20x до
    // 1,5x. Поэтому бралось максимальное по текстам.
    //
    // При нулевом пределе выбор статистики не решает ничего: честный
    // узел класса даёт 0 на КАЖДОМ тексте, а отклонившийся — не 0, и
    // любая статистика это видит. Проверяется теперь именно это: шести
    // миллионных на одном тексте довольно, чтобы пробу не сдать.
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::RegisterExecutor {
            model: MODEL.into(), runtime: RUNTIME.into(),
            dtype: DTYPE.into(), card_class: ARCH.into(),
            kernel_cache_hash: CACHE.into(),
        }).unwrap();
    let err = execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::SubmitConformance {
            divergence_micro: TINY,
            logprobs_hash: commit_hash(&probe_foreign()),
        }).unwrap_err();
    assert!(matches!(err, ContractError::ConformanceFailed { got: TINY, limit: 0 }),
        "{err:?}");
}

#[test]
fn chestnuyu_probu_oproverzhenie_podtverzhdaet() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let res = execute(deps.as_mut(), mock_env(), mock_info("anyone", &[]),
        ExecuteMsg::DisputeConformance {
            executor: EXEC.into(),
            reference_logprobs: probe_ref(),
            executor_logprobs: probe_honest(),
        }).unwrap();
    assert!(res.attributes.iter().any(|a| a.key == "lied" && a.value == "false"));
    let e: Executor = from_json(query(deps.as_ref(), mock_env(),
        QueryMsg::Executor { address: EXEC.into() }).unwrap()).unwrap();
    assert_eq!(e.status, ExecutorStatus::Active, "честного опровержение не трогает");
    assert_eq!(e.conformance.unwrap().divergence_micro, 0,
        "честный узел класса заявляет ноль, и опровержение это подтверждает");
}

#[test]
fn oproverzhenie_podsunutym_etalonom_ne_proydyot() {
    // Иначе обвинить честного можно было бы, подставив другой эталон.
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let mut fake = probe_ref();
    fake[0] -= 900_000;
    let err = execute(deps.as_mut(), mock_env(), mock_info("anyone", &[]),
        ExecuteMsg::DisputeConformance {
            executor: EXEC.into(),
            reference_logprobs: fake,
            executor_logprobs: probe_honest(),
        }).unwrap_err();
    assert!(matches!(err, ContractError::RevealMismatch { .. }));
}

#[test]
fn oproverzhenie_chuzhimi_vektorami_ispolnitelya_ne_proydyot() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let mut podmena = probe_honest();
    podmena[0] -= 900_000;
    let err = execute(deps.as_mut(), mock_env(), mock_info("anyone", &[]),
        ExecuteMsg::DisputeConformance {
            executor: EXEC.into(),
            reference_logprobs: probe_ref(),
            executor_logprobs: podmena,
        }).unwrap_err();
    assert!(matches!(err, ContractError::RevealMismatch { .. }));
}

#[test]
fn ne_ta_dlina_v_probe_otvergaetsya() {
    // Оценивается готовый текст, обрываться ему негде: длины заданы
    // текстами пробы. Значит другая длина — признак, что оценивалось не
    // то, и это не допуск, а отказ.
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::RegisterExecutor {
            model: MODEL.into(), runtime: RUNTIME.into(),
            dtype: DTYPE.into(), card_class: ARCH.into(),
            kernel_cache_hash: CACHE.into(),
        }).unwrap();
    let short: Vec<MicroLog> = probe_honest()[..5].to_vec();
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::SubmitConformance {
            divergence_micro: 0,
            logprobs_hash: commit_hash(&short),
        }).unwrap();
    let err = execute(deps.as_mut(), mock_env(), mock_info("anyone", &[]),
        ExecuteMsg::DisputeConformance {
            executor: EXEC.into(),
            reference_logprobs: probe_ref(),
            executor_logprobs: short,
        }).unwrap_err();
    assert!(matches!(err, ContractError::ConformanceShape { got: 5, want: 6 }));
}

#[test]
fn pustoy_etalon_ne_prinimaetsya() {
    // Пустой эталон означал бы пробу, которую проходят не считая ничего.
    let mut deps = mock_dependencies();
    let err = instantiate(deps.as_mut(), mock_env(), mock_info(ADMIN, &[]),
        InstantiateMsg {
            admin: ADMIN.into(),
            checks_required: CHECKS,
            pinned_arch: ARCH.into(), kernel_cache_hash: CACHE.into(),
    pinned_model: MODEL.into(),
            pinned_runtime: RUNTIME.into(), pinned_dtype: DTYPE.into(),
            conformance_limit_micro: CONF_LIMIT,
            conformance_ref_hash: commit_hash(&probe_ref()),
            conformance_lens: vec![],
            claim_ttl_seconds: CLAIM_TTL, check_ttl_seconds: CHECK_TTL,
            settlement: Settlement::Cw20(Addr::unchecked(CW20)),
            stake_denom: STAKE.into(), min_stake: Uint128::new(MIN_STAKE),
            verifier_fee_bps: FEE_BPS, forfeit: Forfeit::Burn,
        }).unwrap_err();
    assert!(matches!(err, ContractError::EmptyProbe));
}

#[test]
fn zabanennyy_probu_ne_sdast() {
    // Иначе пойманный на подлоге сдаёт пробу заново и возвращается.
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    submit_checks_mismatching(deps.as_mut(), id, CHECKS as usize);
    execute(deps.as_mut(), mock_env(), mock_info("anyone", &[]),
        ExecuteMsg::Finalize { job_id: id }).unwrap();
    let err = execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::SubmitConformance {
            divergence_micro: HONEST_MEAN,
            logprobs_hash: commit_hash(&probe_honest()),
        }).unwrap_err();
    assert!(matches!(err, ContractError::ExecutorBanned));
}

#[test]
fn proveryayushchiy_bez_proby_ne_prinimaetsya() {
    // Проверяющий в неверном режиме исполнения завышает расхождение и
    // обвиняет честного. Правило «виновен, если превысили все» от этого
    // не спасает: четыре неверно настроенных вынесут вердикт согласованно.
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    let err = execute(deps.as_mut(), mock_env(), mock_info("nobody", &[]),
        ExecuteMsg::SubmitCheck {
            job_id: id, divergence_micro: HUGE, logprobs_hash: "h".into(),
        }).unwrap_err();
    assert!(matches!(err, ContractError::ExecutorUnknown));
}

#[test]
fn proveryayushchiy_so_staroy_proboy_ne_prinimaetsya() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    register_and_probe(deps.as_mut(), "checker0");
    // Сменили эталон — проба проверяющего больше не о действующей настройке.
    execute(deps.as_mut(), mock_env(), mock_info(ADMIN, &[]),
        ExecuteMsg::UpdateConfig {
            admin: None, checks_required: None,
            pinned_arch: None, kernel_cache_hash: None,
            pinned_model: None, pinned_runtime: None,
            pinned_dtype: None, conformance_limit_micro: None,
            conformance_ref: Some(ConformanceRef {
                ref_hash: commit_hash(&probe_foreign()),
                lens: PROBE_LENS.to_vec(),
            }),
            claim_ttl_seconds: None, check_ttl_seconds: None,
            settlement: None, stake_denom: None, min_stake: None,
            verifier_fee_bps: None, forfeit: None,
        }).unwrap();
    let err = execute(deps.as_mut(), mock_env(), mock_info("checker0", &[]),
        ExecuteMsg::SubmitCheck {
            job_id: id, divergence_micro: HONEST_MEAN, logprobs_hash: "h".into(),
        }).unwrap_err();
    assert!(matches!(err, ContractError::ConformanceStale { have: 1, want: 2 }));
}

// --- расчёт родной монетой ------------------------------------------------
//
// Зачем он понадобился: шлюз этапа 1 собирает родной AXM, а контракт умел
// только CW20. Разные монеты на двух этапах пришлось бы объяснять каждому
// владельцу карты, а BARTR для выплат слаб — выпуск разрешает сервер, и
// 40% лежит на одном адресе.
//
// Вид расчёта — НАСТРОЙКА, а не два контракта: при появлении IBC расчёт
// переедет на ваучер, то есть на третью единицу.

const NATIVE: &str = "uaxm";

fn setup_native(deps: DepsMut) {
    instantiate(
        deps,
        mock_env(),
        mock_info(ADMIN, &[]),
        InstantiateMsg {
            admin: ADMIN.into(),
            checks_required: CHECKS,
            pinned_model: MODEL.into(),
            pinned_runtime: RUNTIME.into(),
            pinned_dtype: DTYPE.into(),
            pinned_arch: ARCH.into(),
            kernel_cache_hash: CACHE.into(),
            conformance_limit_micro: CONF_LIMIT,
            conformance_ref_hash: commit_hash(&probe_ref()),
            conformance_lens: PROBE_LENS.to_vec(),
            claim_ttl_seconds: CLAIM_TTL,
            check_ttl_seconds: CHECK_TTL,
            settlement: Settlement::Native(NATIVE.into()),
            stake_denom: STAKE.into(),
            min_stake: Uint128::new(MIN_STAKE),
            verifier_fee_bps: FEE_BPS,
            forfeit: Forfeit::Burn,
        },
    )
    .unwrap();
}

/// Заказчик ставит задание и платит родной монетой — она идёт с транзакцией.
fn submit_job_native(deps: DepsMut) {
    execute(
        deps,
        mock_env(),
        mock_info(CLIENT, &coins(TOTAL, NATIVE)),
        ExecuteMsg::SubmitJob {
            prompt_hash: "ph1".into(), prompt_uri: PROMPT_URI.into(),
            price: Uint128::new(PRICE),
        },
    )
    .unwrap();
}

fn native_job_until_answered(deps: DepsMut) -> u64 {
    let mut d = deps;
    submit_job_native(d.branch());
    let id = 1;
    execute(d.branch(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::ClaimJob { job_id: id }).unwrap();
    execute(d.branch(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::SubmitAnswer {
            job_id: id, answer_hash: "ah".into(),
            logprobs_hash: "lh".into(), answer_tokens: 120,
        }).unwrap();
    id
}

#[test]
fn rodnoy_platyozh_stavit_zadanie() {
    let mut deps = mock_dependencies();
    setup_native(deps.as_mut());
    submit_job_native(deps.as_mut());
    let job: Job = from_json(query(deps.as_ref(), mock_env(),
        QueryMsg::Job { job_id: 1 }).unwrap()).unwrap();
    assert_eq!(job.price, Uint128::new(PRICE));
    assert_eq!(job.state, JobState::Open);
}

#[test]
fn bez_deneg_zadaniya_net() {
    let mut deps = mock_dependencies();
    setup_native(deps.as_mut());
    let err = execute(deps.as_mut(), mock_env(), mock_info(CLIENT, &[]),
        ExecuteMsg::SubmitJob { prompt_hash: "ph".into(), prompt_uri: PROMPT_URI.into(),
            price: Uint128::new(PRICE) }).unwrap_err();
    assert!(matches!(err, ContractError::WrongSettlementFunds { ref got, .. }
        if got == "ничего"));
}

#[test]
fn chuzhaya_moneta_ne_oplachivaet() {
    let mut deps = mock_dependencies();
    setup_native(deps.as_mut());
    let err = execute(deps.as_mut(), mock_env(),
        mock_info(CLIENT, &coins(TOTAL, "uatom")),
        ExecuteMsg::SubmitJob { prompt_hash: "ph".into(), prompt_uri: PROMPT_URI.into(),
            price: Uint128::new(PRICE) }).unwrap_err();
    assert!(matches!(err, ContractError::WrongSettlementFunds { .. }));
}

#[test]
fn chuzhaya_moneta_ryadom_s_nuzhnoy_otvergaetsya() {
    // Она осталась бы в контракте навсегда: вернуть её некому и нечем.
    let mut deps = mock_dependencies();
    setup_native(deps.as_mut());
    let mut funds = coins(TOTAL, NATIVE);
    funds.push(cosmwasm_std::Coin {
        denom: "uatom".into(),
        amount: Uint128::new(5),
    });
    funds.sort_by(|a, b| a.denom.cmp(&b.denom));
    let err = execute(deps.as_mut(), mock_env(), mock_info(CLIENT, &funds),
        ExecuteMsg::SubmitJob { prompt_hash: "ph".into(), prompt_uri: PROMPT_URI.into(),
            price: Uint128::new(PRICE) }).unwrap_err();
    assert!(matches!(err, ContractError::WrongSettlementFunds { .. }));
}

#[test]
fn cw20_pri_rodnom_raschyote_ne_prinimaetsya() {
    // Иначе задание оказалось бы оплачено не тем, чем платят все.
    let mut deps = mock_dependencies();
    setup_native(deps.as_mut());
    let err = execute(deps.as_mut(), mock_env(), mock_info(CW20, &[]),
        ExecuteMsg::Receive(Cw20ReceiveMsg {
            sender: CLIENT.into(),
            amount: Uint128::new(TOTAL),
            msg: to_json_binary(&ReceiveHook::SubmitJob {
                prompt_hash: "ph".into(), prompt_uri: PROMPT_URI.into(),
                price: Uint128::new(PRICE) }).unwrap(),
        })).unwrap_err();
    assert!(matches!(err, ContractError::NotCw20Settlement));
}

#[test]
fn rodnoy_platyozh_pri_raschyote_cw20_ne_prinimaetsya() {
    // Два пути для одних денег — это две дыры вместо одной двери.
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    let err = execute(deps.as_mut(), mock_env(),
        mock_info(CLIENT, &coins(TOTAL, NATIVE)),
        ExecuteMsg::SubmitJob { prompt_hash: "ph".into(), prompt_uri: PROMPT_URI.into(),
            price: Uint128::new(PRICE) }).unwrap_err();
    assert!(matches!(err, ContractError::NotNativeSettlement { .. }));
}

#[test]
fn rodnye_vyplaty_idut_bankmsg_i_summy_te_zhe() {
    let mut deps = mock_dependencies();
    setup_native(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let id = native_job_until_answered(deps.as_mut());
    submit_checks_matching(deps.as_mut(), id, CHECKS as usize);
    let res = execute(deps.as_mut(), mock_env(), mock_info("anyone", &[]),
        ExecuteMsg::Finalize { job_id: id }).unwrap();
    // Те же 970 bps: на четверых по 24 250, исполнителю ЦЕНА ЦЕЛИКОМ.
    assert!(res.attributes.iter().any(|a| a.key == "verifier_fees"
        && a.value == FEE.to_string()));
    assert!(res.attributes.iter().any(|a| a.key == "settled"
        && a.value == PRICE.to_string()));
    assert_eq!(res.messages.len(), 3);
    // Проверка по ТИПУ сообщения, а не по отладочной строке: строка —
    // представление, и меняется она не вместе с поведением.
    let mut sent = 0u128;
    for m in &res.messages {
        match &m.msg {
            cosmwasm_std::CosmosMsg::Bank(cosmwasm_std::BankMsg::Send {
                amount, ..
            }) => {
                assert_eq!(amount.len(), 1);
                assert_eq!(amount[0].denom, NATIVE, "в том самом денoме");
                sent += amount[0].amount.u128();
            }
            other => panic!("выплата не родной монетой: {other:?}"),
        }
    }
    assert_eq!(sent, TOTAL, "разошлось всё внесённое заказчиком, без остатка");
}

#[test]
fn rodnoy_vozvrat_zakazchiku_pri_podloge() {
    let mut deps = mock_dependencies();
    setup_native(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let id = native_job_until_answered(deps.as_mut());
    submit_checks_mismatching(deps.as_mut(), id, CHECKS as usize);
    let res = execute(deps.as_mut(), mock_env(), mock_info("anyone", &[]),
        ExecuteMsg::Finalize { job_id: id }).unwrap();
    assert!(res.attributes.iter().any(|a| a.key == "guilty" && a.value == "true"));
    let dump = format!("{:?}", res.messages);
    assert!(dump.contains(CLIENT), "заказчику вернули");
    assert!(dump.contains(STAKE), "залог изъят в своём денoме");
    let e: Executor = from_json(query(deps.as_ref(), mock_env(),
        QueryMsg::Executor { address: EXEC.into() }).unwrap()).unwrap();
    assert_eq!(e.stake, Uint128::zero());
}

#[test]
fn vid_raschyota_menyaetsya_nastroykoy() {
    // Переезд расчёта на другую единицу — правка настройки, а не выпуск
    // нового контракта. При появлении IBC это будет ваучер.
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    execute(deps.as_mut(), mock_env(), mock_info(ADMIN, &[]),
        ExecuteMsg::UpdateConfig {
            admin: None, checks_required: None,
            pinned_arch: None, kernel_cache_hash: None,
            pinned_model: None, pinned_runtime: None,
            pinned_dtype: None, conformance_limit_micro: None,
            conformance_ref: None,
            claim_ttl_seconds: None,
            check_ttl_seconds: None,
            settlement: Some(Settlement::Native(NATIVE.into())),
            stake_denom: None, min_stake: None,
            verifier_fee_bps: None, forfeit: None,
        }).unwrap();
    // Теперь принимается родной платёж, а CW20 — нет.
    submit_job_native(deps.as_mut());
    let err = execute(deps.as_mut(), mock_env(), mock_info(CW20, &[]),
        ExecuteMsg::Receive(Cw20ReceiveMsg {
            sender: CLIENT.into(), amount: Uint128::new(TOTAL),
            msg: to_json_binary(&ReceiveHook::SubmitJob {
                prompt_hash: "ph".into(), prompt_uri: PROMPT_URI.into(),
                price: Uint128::new(PRICE) }).unwrap(),
        })).unwrap_err();
    assert!(matches!(err, ContractError::NotCw20Settlement));
}

#[test]
fn imya_edinicy_vidno_v_nastroykah() {
    // Демону нужно знать, чем расчёт, не разбирая вариант руками.
    assert_eq!(Settlement::Native(NATIVE.into()).label(), NATIVE);
    assert_eq!(Settlement::Cw20(Addr::unchecked(CW20)).label(),
               format!("cw20:{CW20}"));
}


#[test]
fn list_razvyortyvaniya_razbiraetsya_kontraktom() {
    // Лист в deploy/instantiate.json — то, чем контракт будут разворачивать
    // на самом деле. Если он разойдётся с формой сообщения, это выяснится
    // при развёртывании, на живой цепочке и за газ. Пусть выясняется здесь.
    //
    // Адреса в листе — заглушки: InstantiateMsg принимает их строками, а
    // проверяет уже контракт при instantiate. Здесь проверяется ФОРМА.
    let raw = include_str!("../deploy/instantiate.json");
    let msg: InstantiateMsg = serde_json::from_str(raw).expect("лист не разобран");
    assert_eq!(msg.checks_required, CHECKS, "двух проверок хватает");
    assert_eq!(msg.pinned_model, MODEL);
    assert_eq!(msg.pinned_runtime, RUNTIME);
    assert_eq!(msg.pinned_dtype, DTYPE);
    assert_eq!(msg.pinned_arch, ARCH, "класс назван в листе");
    assert_eq!(msg.kernel_cache_hash, CACHE,
               "кеш ядер тот, что опубликован в daemon/kernel-cache/sm_89");
    assert_eq!(msg.conformance_limit_micro, 0,
               "предел пробы нулевой: внутри класса честное расхождение ровно 0");
    // Сроки. Без них взятое и брошенное задание висело бы вечно, запирая
    // деньги заказчика; нулевой срок — наоборот, позволял бы отобрать
    // задание в том же блоке.
    assert_eq!(msg.claim_ttl_seconds, CLAIM_TTL, "срок на ответ");
    assert_eq!(msg.check_ttl_seconds, CHECK_TTL, "срок на сбор проверок");
    assert_eq!(msg.verifier_fee_bps, FEE_BPS);
    // Решено 14 октября: залог 30 400 AXM, расчёт родной монетой,
    // конфискат по адресу (BankMsg::Burn на самой Axiome не проверен).
    assert_eq!(msg.min_stake, Uint128::new(30_400_000_000));
    assert!(matches!(msg.settlement, Settlement::Native(ref d) if d == "uaxm"));
    assert!(matches!(msg.forfeit, Forfeit::ToAddress(_)));
    // Эталон пробы в листе обязан совпадать с тем, что лежит у демонов:
    // иначе узлы считают пробу против одного набора, а контракт ждёт
    // другого, и честный узел получает отказ.
    assert_eq!(msg.conformance_lens.len(), 34, "34 текста пробы");
    assert_eq!(
        msg.conformance_ref_hash,
        "ed6a265b272006ac9279a7fee66c7e243ed6586fe87b0bd6526257c83a7da2b1",
        "эталон пробы тот же, что в daemon/probe/probe-v2-sm89.json"
    );
}

#[test]
fn listy_klassov_otlichayutsya_tolko_klassom() {
    // Развёрнуто три экземпляра одного кода: по одному на модель карты.
    // Отличать их имеет право РОВНО три поля — класс, кеш ядер и эталон
    // пробы. Разойдись у них залог, доля или предел пробы — и сеть
    // распалась бы на классы с разными правилами, чего никто не заметил
    // бы до жалобы узла. Поэтому сверяется и то, что совпадает.
    let listy: [(&str, &str, &str, &str); 3] = [
        (include_str!("../deploy/instantiate.json"), "sm_89",
         "3753554454f2c9b850c6ec0912a58e39ee08028012f3de1e9649e62d67cc131a",
         "ed6a265b272006ac9279a7fee66c7e243ed6586fe87b0bd6526257c83a7da2b1"),
        (include_str!("../deploy/instantiate-sm86.json"), "sm_86",
         "9b1e2654869c5af7826d8f9427641ac8b9dfacdad04d8add408d11eb94555698",
         "e0ff32b3e1cd41230ccfa6749e4581d8d43608e7559f130bb3c9e108b8fddaad"),
        (include_str!("../deploy/instantiate-sm120.json"), "sm_120",
         "c78d15d82f018c3ec29885d43720c07b0342cd06421c12237efa1b30f28e1a32",
         "097293ab113371537180fc5d94f754419ac7e04cf4165b6d255b689228594a07"),
    ];
    let mut razobrannye = Vec::new();
    for (raw, arch, _cache, _ref_hash) in listy.iter() {
        let m: InstantiateMsg = serde_json::from_str(raw)
            .unwrap_or_else(|e| panic!("лист класса {arch} не разобран: {e}"));
        assert_eq!(&m.pinned_arch, arch, "класс назван в своём листе");
        razobrannye.push(m);
    }
    let base = &razobrannye[0];
    for (m, (_, arch, _, _)) in razobrannye.iter().zip(listy.iter()).skip(1) {
        assert_eq!(m.checks_required, base.checks_required, "{arch}: проверок");
        assert_eq!(m.pinned_model, base.pinned_model, "{arch}: модель");
        assert_eq!(m.pinned_runtime, base.pinned_runtime, "{arch}: рантайм");
        assert_eq!(m.pinned_dtype, base.pinned_dtype, "{arch}: точность");
        assert_eq!(m.min_stake, base.min_stake, "{arch}: залог");
        assert_eq!(m.verifier_fee_bps, base.verifier_fee_bps, "{arch}: доля");
        assert_eq!(m.conformance_limit_micro, base.conformance_limit_micro,
                   "{arch}: предел пробы");
        assert_eq!(m.claim_ttl_seconds, base.claim_ttl_seconds, "{arch}: срок ответа");
        assert_eq!(m.check_ttl_seconds, base.check_ttl_seconds, "{arch}: срок проверок");
        assert_eq!(m.conformance_lens, base.conformance_lens,
                   "{arch}: тексты пробы у классов одни и те же");
        assert_eq!(m.admin, base.admin, "{arch}: админ");
    }
    // Предел нулевой у ВСЕХ: класс меняет эталон, а не строгость.
    for (m, (_, arch, _, _)) in razobrannye.iter().zip(listy.iter()) {
        assert_eq!(m.conformance_limit_micro, 0, "{arch}: допуск нулевой");
        assert_eq!(m.conformance_lens.len(), 34, "{arch}: 34 текста пробы");
    }
    // А три отличающих поля обязаны быть именно опубликованными: кеш из
    // daemon/kernel-cache/<класс>/ и эталон из daemon/probe/.
    for (m, (_, arch, cache, ref_hash)) in razobrannye.iter().zip(listy.iter()) {
        assert_eq!(&m.kernel_cache_hash, cache,
                   "{arch}: кеш ядер тот, что опубликован");
        assert_eq!(&m.conformance_ref_hash, ref_hash,
                   "{arch}: эталон пробы тот, что опубликован");
    }
    // И ни одно из трёх полей не повторяется между классами: иначе два
    // класса считали бы себя одним.
    for i in 0..razobrannye.len() {
        for j in (i + 1)..razobrannye.len() {
            let (a, b) = (&razobrannye[i], &razobrannye[j]);
            assert_ne!(a.pinned_arch, b.pinned_arch);
            assert_ne!(a.kernel_cache_hash, b.kernel_cache_hash);
            assert_ne!(a.conformance_ref_hash, b.conformance_ref_hash);
        }
    }
}

// --- долю платит заказчик -------------------------------------------------
//
// Решено 14 октября. До этого доля вычиталась из цены, и исполнитель
// получал её минус 9,7%: повышение доли голосованием молча урезало бы
// выручку каждой карты. Теперь цена — то, что получит исполнитель, а долю
// заказчик вносит сверху.

#[test]
fn zakazchik_vnosit_cenu_plyus_dolyu() {
    let mut deps = mock_dependencies();
    setup_native(deps.as_mut());
    let res = execute(deps.as_mut(), mock_env(),
        mock_info(CLIENT, &coins(TOTAL, NATIVE)),
        ExecuteMsg::SubmitJob { prompt_hash: "ph".into(), prompt_uri: PROMPT_URI.into(),
            price: Uint128::new(PRICE) }).unwrap();
    assert!(res.attributes.iter().any(|a| a.key == "price"
        && a.value == PRICE.to_string()));
    assert!(res.attributes.iter().any(|a| a.key == "verifier_fee"
        && a.value == FEE.to_string()));
    assert!(res.attributes.iter().any(|a| a.key == "total"
        && a.value == TOTAL.to_string()));
}

#[test]
fn nedoplata_i_pereplata_otvergayutsya() {
    // Меньше — проверки нечем оплатить. Больше — остаток завис бы на
    // контракте, и вернуть его было бы некому.
    let mut deps = mock_dependencies();
    setup_native(deps.as_mut());
    for sent in [PRICE, TOTAL - 1, TOTAL + 1] {
        let err = execute(deps.as_mut(), mock_env(),
            mock_info(CLIENT, &coins(sent, NATIVE)),
            ExecuteMsg::SubmitJob { prompt_hash: "ph".into(), prompt_uri: PROMPT_URI.into(),
                price: Uint128::new(PRICE) }).unwrap_err();
        assert!(matches!(err, ContractError::WrongTotal { .. }),
            "сумма {sent} принята, а не должна");
    }
}

#[test]
fn nulevaya_cena_otvergaetsya() {
    let mut deps = mock_dependencies();
    setup_native(deps.as_mut());
    let err = execute(deps.as_mut(), mock_env(),
        mock_info(CLIENT, &coins(1, NATIVE)),
        ExecuteMsg::SubmitJob { prompt_hash: "ph".into(), prompt_uri: PROMPT_URI.into(),
            price: Uint128::zero() }).unwrap_err();
    assert!(matches!(err, ContractError::ZeroPrice));
}

#[test]
fn ispolnitel_poluchaet_cenu_celikom() {
    let mut deps = mock_dependencies();
    setup_native(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let id = native_job_until_answered(deps.as_mut());
    submit_checks_matching(deps.as_mut(), id, CHECKS as usize);
    let res = execute(deps.as_mut(), mock_env(), mock_info("anyone", &[]),
        ExecuteMsg::Finalize { job_id: id }).unwrap();
    assert!(res.attributes.iter().any(|a| a.key == "settled"
        && a.value == PRICE.to_string()), "цена целиком, без вычета доли");
    assert!(res.attributes.iter().any(|a| a.key == "verifier_fees"
        && a.value == FEE.to_string()));
}

#[test]
fn povyshenie_doli_ne_urezaet_vyruchku_ispolnitelya() {
    // САМОЕ важное свойство этой правки. Доля — параметр, и её могут
    // сменить между постановкой задания и вердиктом. Деньги к тому
    // времени уже внесены, поэтому плата берётся ИЗ ЗАДАНИЯ, а не
    // пересчитывается по новой доле.
    let mut deps = mock_dependencies();
    setup_native(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let id = native_job_until_answered(deps.as_mut());
    execute(deps.as_mut(), mock_env(), mock_info(ADMIN, &[]),
        ExecuteMsg::UpdateConfig {
            admin: None, checks_required: None,
            pinned_arch: None, kernel_cache_hash: None,
            pinned_model: None, pinned_runtime: None,
            pinned_dtype: None, conformance_limit_micro: None,
            conformance_ref: None,
            claim_ttl_seconds: None, check_ttl_seconds: None,
            settlement: None, stake_denom: None,
            min_stake: None, verifier_fee_bps: Some(5_000), forfeit: None,
        }).unwrap();
    submit_checks_matching(deps.as_mut(), id, CHECKS as usize);
    let res = execute(deps.as_mut(), mock_env(), mock_info("anyone", &[]),
        ExecuteMsg::Finalize { job_id: id }).unwrap();
    assert!(res.attributes.iter().any(|a| a.key == "settled"
        && a.value == PRICE.to_string()),
        "доля выросла до 50%, а исполнитель получил цену целиком");
    assert!(res.attributes.iter().any(|a| a.key == "verifier_fees"
        && a.value == FEE.to_string()),
        "проверяющим ушло столько, сколько внесли при постановке");
}

#[test]
fn dolya_delitsya_vverh_a_ostatok_vozvrashchaetsya_zakazchiku() {
    // Цена 1: доля 970/10000 = 0,097, вверх это 1. Поделить одну единицу
    // на четверых нельзя, и она возвращается ЗАКАЗЧИКУ: он внёс её за
    // проверку, проверка её не получила — оставлять исполнителю не за что.
    let mut deps = mock_dependencies();
    setup_native(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    execute(deps.as_mut(), mock_env(), mock_info(CLIENT, &coins(2, NATIVE)),
        ExecuteMsg::SubmitJob { prompt_hash: "ph".into(), prompt_uri: PROMPT_URI.into(),
            price: Uint128::one() }).unwrap();
    let id = 1;
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::ClaimJob { job_id: id }).unwrap();
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::SubmitAnswer { job_id: id, answer_hash: "a".into(),
            logprobs_hash: "l".into(), answer_tokens: 120 }).unwrap();
    submit_checks_matching(deps.as_mut(), id, CHECKS as usize);
    let res = execute(deps.as_mut(), mock_env(), mock_info("anyone", &[]),
        ExecuteMsg::Finalize { job_id: id }).unwrap();
    assert!(res.attributes.iter().any(|a| a.key == "verifier_fees"
        && a.value == "0"), "одну единицу на четверых не поделить");
    assert!(res.attributes.iter().any(|a| a.key == "fee_returned"
        && a.value == "1"), "неразошедшаяся плата вернулась заказчику");
    // Ничьих денег на контракте не остаётся: 1 исполнителю, 1 заказчику.
    let mut sent = 0u128;
    for m in &res.messages {
        if let cosmwasm_std::CosmosMsg::Bank(cosmwasm_std::BankMsg::Send {
            amount, ..
        }) = &m.msg
        {
            sent += amount[0].amount.u128();
        }
    }
    assert_eq!(sent, 2, "разошлось всё внесённое");
}

#[test]
fn pri_podloge_zakazchiku_vozvrashchaetsya_cena_a_ne_vsyo() {
    // Проверяющим платят независимо от вердикта: они работу сделали.
    let mut deps = mock_dependencies();
    setup_native(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let id = native_job_until_answered(deps.as_mut());
    submit_checks_mismatching(deps.as_mut(), id, CHECKS as usize);
    let res = execute(deps.as_mut(), mock_env(), mock_info("anyone", &[]),
        ExecuteMsg::Finalize { job_id: id }).unwrap();
    assert!(res.attributes.iter().any(|a| a.key == "guilty" && a.value == "true"));
    assert!(res.attributes.iter().any(|a| a.key == "verifier_fees"
        && a.value == FEE.to_string()), "проверяющим заплатили");
    assert!(res.attributes.iter().any(|a| a.key == "settled"
        && a.value == PRICE.to_string()), "заказчику вернулась цена");
}

#[test]
fn administratora_mozhno_smenit() {
    // Без этого потеря ключа навсегда замораживает ВСЕ параметры: ни
    // порог, ни эталон пробы, ни долю уже не поправить. Особенно важно,
    // когда ключ лежит в файле, а не в кошельке с резервной копией.
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    let new_admin = "axm1e4r6zyrwj5xltetdm0eap5zxldh577lh564l67";
    execute(deps.as_mut(), mock_env(), mock_info(ADMIN, &[]),
        ExecuteMsg::UpdateConfig {
            admin: Some(new_admin.into()),
            checks_required: None,
            pinned_arch: None, kernel_cache_hash: None,
            pinned_model: None, pinned_runtime: None,
            pinned_dtype: None, conformance_limit_micro: None,
            conformance_ref: None,
            claim_ttl_seconds: None, check_ttl_seconds: None,
            settlement: None, stake_denom: None,
            min_stake: None, verifier_fee_bps: None, forfeit: None,
        }).unwrap();
    // Прежний администратор больше ничего не правит.
    let err = execute(deps.as_mut(), mock_env(), mock_info(ADMIN, &[]),
        ExecuteMsg::UpdateConfig {
            admin: None,
            checks_required: Some(1), pinned_arch: None, kernel_cache_hash: None,
            pinned_model: None, pinned_runtime: None, pinned_dtype: None,
            conformance_limit_micro: None, conformance_ref: None,
            claim_ttl_seconds: None, check_ttl_seconds: None,
            settlement: None, stake_denom: None, min_stake: None,
            verifier_fee_bps: None, forfeit: None,
        }).unwrap_err();
    assert!(matches!(err, ContractError::NotAdmin));
    // А новый — правит.
    execute(deps.as_mut(), mock_env(), mock_info(new_admin, &[]),
        ExecuteMsg::UpdateConfig {
            admin: None,
            checks_required: Some(3), pinned_arch: None, kernel_cache_hash: None,
            pinned_model: None, pinned_runtime: None, pinned_dtype: None,
            conformance_limit_micro: None, conformance_ref: None,
            claim_ttl_seconds: None, check_ttl_seconds: None,
            settlement: None, stake_denom: None, min_stake: None,
            verifier_fee_bps: None, forfeit: None,
        }).unwrap();
}

// --- перенос на новый код -------------------------------------------------
//
// Добавлено 26 октября вместе с точкой входа. Проверяется ровно то, что
// будет сделано на живой цепочке: в хранилище лежат октябрьские
// настройки с `attestor` и БЕЗ записи о версии, и перенос обязан
// превратить их в нынешние, ничего больше не тронув.

/// Записать в хранилище настройки ПРЕЖНЕЙ формы — так, как они лежат у
/// трёх развёрнутых экземпляров. Версия нарочно не пишется: октябрьский
/// код её не писал.
fn put_v1_config(deps: DepsMut) {
    let v1 = serde_json::json!({
        "admin": ADMIN,
        "attestor": "attestor",
        "checks_required": CHECKS,
        "pinned_model": MODEL,
        "pinned_runtime": RUNTIME,
        "pinned_dtype": DTYPE,
        "pinned_arch": ARCH,
        "kernel_cache_hash": CACHE,
        "conformance_limit_micro": CONF_LIMIT,
        "conformance_ref_hash": commit_hash(&probe_ref()),
        "conformance_lens": PROBE_LENS.to_vec(),
        "conformance_version": 1,
        "settlement": {"native": STAKE},
        "stake_denom": STAKE,
        "min_stake": MIN_STAKE.to_string(),
        "verifier_fee_bps": FEE_BPS,
        "forfeit": {"to_address": "forfeit_address"},
    });
    deps.storage
        .set(b"config", serde_json::to_vec(&v1).unwrap().as_slice());
}

#[test]
fn perenos_sbrasyvaet_attestora_i_hranit_ostalnoe() {
    let mut deps = mock_dependencies();
    put_v1_config(deps.as_mut());
    assert!(STATE_VERSION.may_load(deps.as_ref().storage).unwrap().is_none(),
            "у октябрьского хранилища записи о версии нет");

    let res = migrate(deps.as_mut(), mock_env(), MigrateMsg {}).unwrap();
    assert!(res.attributes.iter().any(|a| a.key == "from" && a.value == "1"));
    assert!(res
        .attributes
        .iter()
        .any(|a| a.key == "changed"
            && a.value == "attestor_dropped,ttl_added,endpoint,prompt_uri,jobs_query"));
    assert_eq!(
        STATE_VERSION.load(deps.as_ref().storage).unwrap(),
        CURRENT_VERSION
    );

    // Настройки читаются нынешней формой, и всё, кроме аттестора, цело.
    let cfg: Config = from_json(
        query(deps.as_ref(), mock_env(), QueryMsg::Config {}).unwrap()).unwrap();
    assert_eq!(cfg.admin, Addr::unchecked(ADMIN));
    assert_eq!(cfg.checks_required, CHECKS);
    assert_eq!(cfg.pinned_arch, ARCH);
    assert_eq!(cfg.kernel_cache_hash, CACHE);
    assert_eq!(cfg.conformance_limit_micro, CONF_LIMIT);
    assert_eq!(cfg.conformance_ref_hash, commit_hash(&probe_ref()));
    assert_eq!(cfg.conformance_lens, PROBE_LENS.to_vec());
    // Версия пробы НЕ сбрасывается переносом: выданные пробы остаются
    // действительными, иначе перенос вышиб бы из сети всех честных узлов.
    assert_eq!(cfg.conformance_version, 1);
    // Сроки появились со значениями переносчика, а не нулевыми: нулевой
    // срок означал бы, что задание отбирают в том же блоке, что взяли.
    assert_eq!(cfg.claim_ttl_seconds, 600);
    assert_eq!(cfg.check_ttl_seconds, 3600);
    assert_eq!(cfg.min_stake, Uint128::new(MIN_STAKE));
    assert_eq!(cfg.verifier_fee_bps, FEE_BPS);
    assert!(matches!(cfg.forfeit, Forfeit::ToAddress(_)));
}

#[test]
fn povtornyy_perenos_bezvreden() {
    // Отправить перенос дважды — обычное дело: первая отправка могла
    // потеряться в сети. Вторая обязана ничего не менять, а не падать.
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    let before: Config = from_json(
        query(deps.as_ref(), mock_env(), QueryMsg::Config {}).unwrap()).unwrap();
    let res = migrate(deps.as_mut(), mock_env(), MigrateMsg {}).unwrap();
    assert!(res
        .attributes
        .iter()
        .any(|a| a.key == "changed" && a.value == "nothing"));
    let after: Config = from_json(
        query(deps.as_ref(), mock_env(), QueryMsg::Config {}).unwrap()).unwrap();
    assert_eq!(before, after, "повторный перенос настроек не тронул");
}

#[test]
fn perenos_nazad_i_v_neizvestnoe_otvergaetsya() {
    // Код не берётся переносить форму, которой не знает: откат на старый
    // код после правки хранилища испортил бы настройки молча.
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    STATE_VERSION
        .save(deps.as_mut().storage, &(CURRENT_VERSION + 1))
        .unwrap();
    let err = migrate(deps.as_mut(), mock_env(), MigrateMsg {}).unwrap_err();
    assert!(matches!(err, ContractError::MigrateBackwards { .. }), "вышло {err:?}");
}

#[test]
fn perenos_bez_nastroek_otkazyvaetsya() {
    // Пустое хранилище — не повод что-то придумывать.
    let mut deps = mock_dependencies();
    let err = migrate(deps.as_mut(), mock_env(), MigrateMsg {}).unwrap_err();
    assert!(matches!(err, ContractError::MigrateNoConfig), "вышло {err:?}");
}

#[test]
fn perenos_ne_tronul_ispolnitelya_i_zadaniya() {
    // Переносится только форма настроек. Исполнители, их залоги, пробы и
    // открытые задания обязаны остаться как были — иначе перенос стоил бы
    // сети всей работы, идущей в этот момент.
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    let e_before: Executor = from_json(query(deps.as_ref(), mock_env(),
        QueryMsg::Executor { address: EXEC.into() }).unwrap()).unwrap();
    let j_before: Job = from_json(query(deps.as_ref(), mock_env(),
        QueryMsg::Job { job_id: id }).unwrap()).unwrap();

    migrate(deps.as_mut(), mock_env(), MigrateMsg {}).unwrap();

    let e_after: Executor = from_json(query(deps.as_ref(), mock_env(),
        QueryMsg::Executor { address: EXEC.into() }).unwrap()).unwrap();
    let j_after: Job = from_json(query(deps.as_ref(), mock_env(),
        QueryMsg::Job { job_id: id }).unwrap()).unwrap();
    assert_eq!(e_before, e_after, "исполнитель цел");
    assert_eq!(j_before, j_after, "задание цело");
    // И задание доводится до конца уже после переноса.
    submit_checks_matching(deps.as_mut(), id, CHECKS as usize);
    execute(deps.as_mut(), mock_env(), mock_info("anyone", &[]),
        ExecuteMsg::Finalize { job_id: id }).unwrap();
}

use crate::state::STATE_VERSION as STATE_VERSION_FOR_TEST;

/// Записать настройки формы 2 — те, что лежат у трёх развёрнутых
/// экземпляров с 26 октября: без `attestor`, но и без сроков.
fn put_v2_config(deps: DepsMut) {
    let v2 = serde_json::json!({
        "admin": ADMIN,
        "checks_required": CHECKS,
        "pinned_model": MODEL,
        "pinned_runtime": RUNTIME,
        "pinned_dtype": DTYPE,
        "pinned_arch": ARCH,
        "kernel_cache_hash": CACHE,
        "conformance_limit_micro": CONF_LIMIT,
        "conformance_ref_hash": commit_hash(&probe_ref()),
        "conformance_lens": PROBE_LENS.to_vec(),
        "conformance_version": 7,
        "settlement": {"native": STAKE},
        "stake_denom": STAKE,
        "min_stake": MIN_STAKE.to_string(),
        "verifier_fee_bps": FEE_BPS,
        "forfeit": {"to_address": "forfeit_address"},
    });
    deps.storage
        .set(b"config", serde_json::to_vec(&v2).unwrap().as_slice());
}

#[test]
fn perenos_formy_2_dobavlyaet_sroki() {
    // Форма 2 была у трёх экземпляров до 26 октября; сейчас у них форма
    // 3 (см. `perenos_formy_3_dobavlyaet_adresa`). Тест остаётся: переход
    // 2 -> 4 обязан работать, иначе экземпляр, пропустивший перенос, уже
    // не догонит.
    let mut deps = mock_dependencies();
    put_v2_config(deps.as_mut());
    STATE_VERSION_FOR_TEST
        .save(deps.as_mut().storage, &2u16)
        .unwrap();
    let res = migrate(deps.as_mut(), mock_env(), MigrateMsg {}).unwrap();
    assert!(res.attributes.iter().any(|a| a.key == "from" && a.value == "2"));
    assert!(res
        .attributes
        .iter()
        .any(|a| a.key == "changed"
            && a.value == "ttl_added,endpoint,prompt_uri,jobs_query"));
    let cfg: Config = from_json(
        query(deps.as_ref(), mock_env(), QueryMsg::Config {}).unwrap()).unwrap();
    assert_eq!(cfg.claim_ttl_seconds, 600);
    assert_eq!(cfg.check_ttl_seconds, 3600);
    // Версия пробы взята КАК БЫЛА, а не сброшена в 1: иначе перенос
    // обнулил бы все выданные пробы и вышиб из сети честные узлы.
    assert_eq!(cfg.conformance_version, 7);
    assert_eq!(cfg.verifier_fee_bps, FEE_BPS);
    assert_eq!(cfg.min_stake, Uint128::new(MIN_STAKE));
    assert_eq!(
        STATE_VERSION_FOR_TEST.load(deps.as_ref().storage).unwrap(),
        CURRENT_VERSION
    );
}

// --- сроки заданий ---------------------------------------------------------
//
// Найдено 26 октября при разборе SetSuspended: срока у задания не было
// вовсе, и в трёх случаях деньги заказчика запирались в контракте
// навсегда. Ниже по тесту на каждый.

/// Время через `shift` секунд от обычного.
fn env_after(shift: u64) -> cosmwasm_std::Env {
    let mut e = mock_env();
    e.block.time = e.block.time.plus_seconds(shift);
    e
}

#[test]
fn nevzyatoe_zadanie_zakazchik_zabiraet() {
    let mut deps = mock_dependencies();
    setup_native(deps.as_mut());
    submit_job_native(deps.as_mut());
    // Чужой не забирает.
    let err = execute(deps.as_mut(), mock_env(), mock_info("ne_klient", &[]),
        ExecuteMsg::CancelJob { job_id: 1 }).unwrap_err();
    assert!(matches!(err, ContractError::NotClient), "вышло {err:?}");
    // Заказчик забирает ЦЕНУ И ДОЛЮ: внёс он и то и другое.
    let res = execute(deps.as_mut(), mock_env(), mock_info(CLIENT, &[]),
        ExecuteMsg::CancelJob { job_id: 1 }).unwrap();
    assert!(res.attributes.iter().any(|a| a.key == "refunded"
        && a.value == TOTAL.to_string()), "вернули не всё: {:?}", res.attributes);
    assert_eq!(res.messages.len(), 1, "один перевод заказчику");
    let j: Job = from_json(query(deps.as_ref(), mock_env(),
        QueryMsg::Job { job_id: 1 }).unwrap()).unwrap();
    assert_eq!(j.state, JobState::Settled);
    // Дважды не забрать.
    let err = execute(deps.as_mut(), mock_env(), mock_info(CLIENT, &[]),
        ExecuteMsg::CancelJob { job_id: 1 }).unwrap_err();
    assert!(matches!(err, ContractError::WrongJobState { .. }), "вышло {err:?}");
}

#[test]
fn vzyatoe_zadanie_ne_otmenyaetsya_a_otbiraetsya_po_sroku() {
    let mut deps = mock_dependencies();
    setup_native(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    submit_job_native(deps.as_mut());
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::ClaimJob { job_id: 1 }).unwrap();
    // Пока взято — заказчик не отменяет: иначе он забирал бы деньги у
    // работающего узла.
    let err = execute(deps.as_mut(), mock_env(), mock_info(CLIENT, &[]),
        ExecuteMsg::CancelJob { job_id: 1 }).unwrap_err();
    assert!(matches!(err, ContractError::WrongJobState { .. }), "вышло {err:?}");
    // И до срока не отобрать.
    let err = execute(deps.as_mut(), env_after(CLAIM_TTL), mock_info("kto_ugodno", &[]),
        ExecuteMsg::ReleaseJob { job_id: 1 }).unwrap_err();
    assert!(matches!(err, ContractError::NotExpired { .. }), "вышло {err:?}");
    // После срока отбирает КТО УГОДНО, не админ.
    let res = execute(deps.as_mut(), env_after(CLAIM_TTL + 1), mock_info("kto_ugodno", &[]),
        ExecuteMsg::ReleaseJob { job_id: 1 }).unwrap();
    assert!(res.messages.is_empty(), "деньги остаются в контракте");
    let j: Job = from_json(query(deps.as_ref(), mock_env(),
        QueryMsg::Job { job_id: 1 }).unwrap()).unwrap();
    assert_eq!(j.state, JobState::Open, "задание снова свободно");
    assert_eq!(j.executor, None);
    assert_eq!(j.price, Uint128::new(PRICE), "цена цела");
    // Исполнителю записано брошенное, но НЕ наказание.
    let e: Executor = from_json(query(deps.as_ref(), mock_env(),
        QueryMsg::Executor { address: EXEC.into() }).unwrap()).unwrap();
    assert_eq!(e.abandoned, 1);
    assert_eq!(e.status, ExecutorStatus::Active, "бана нет");
    assert_eq!(e.stake, Uint128::new(MIN_STAKE), "залог цел");
    assert_eq!(e.convictions, 0);
    // И задание берёт следующий — хоть тот же самый.
    execute(deps.as_mut(), env_after(CLAIM_TTL + 2), mock_info(EXEC, &[]),
        ExecuteMsg::ClaimJob { job_id: 1 }).unwrap();
}

#[test]
fn bez_proverok_dengi_vozvrashchayutsya_zakazchiku() {
    let mut deps = mock_dependencies();
    setup_native(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    submit_job_native(deps.as_mut());
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::ClaimJob { job_id: 1 }).unwrap();
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::SubmitAnswer { job_id: 1, answer_hash: "ah".into(),
            logprobs_hash: COMMIT.into(), answer_tokens: 64 }).unwrap();
    // Пришла ОДНА проверка из двух — вердикта нет.
    submit_checks_matching(deps.as_mut(), 1, 1);
    let err = execute(deps.as_mut(), mock_env(), mock_info("anyone", &[]),
        ExecuteMsg::Finalize { job_id: 1 }).unwrap_err();
    assert!(matches!(err, ContractError::NotEnoughChecks { .. }), "вышло {err:?}");
    // До срока закрыть нельзя.
    let err = execute(deps.as_mut(), mock_env(), mock_info("anyone", &[]),
        ExecuteMsg::ExpireChecks { job_id: 1 }).unwrap_err();
    assert!(matches!(err, ContractError::NotExpired { .. }), "вышло {err:?}");
    // После срока: заказчику цена и неразошедшаяся часть доли,
    // пришедшему проверяющему — одна доля из двух.
    let res = execute(deps.as_mut(), env_after(CHECK_TTL + 1), mock_info("anyone", &[]),
        ExecuteMsg::ExpireChecks { job_id: 1 }).unwrap();
    let share = FEE / CHECKS as u128;
    assert!(res.attributes.iter().any(|a| a.key == "verifier_fees"
        && a.value == share.to_string()), "доля пришедшему: {:?}", res.attributes);
    assert!(res.attributes.iter().any(|a| a.key == "refunded"
        && a.value == (TOTAL - share).to_string()),
        "заказчику остальное: {:?}", res.attributes);
    // Исполнитель не получил ничего и не осуждён: вердикта не было.
    let e: Executor = from_json(query(deps.as_ref(), mock_env(),
        QueryMsg::Executor { address: EXEC.into() }).unwrap()).unwrap();
    assert_eq!(e.status, ExecutorStatus::Active, "бана нет");
    assert_eq!(e.stake, Uint128::new(MIN_STAKE), "залог цел");
    assert_eq!(e.jobs_done, 0, "задание не зачтено: вердикта нет");
    assert_eq!(e.convictions, 0);
    let j: Job = from_json(query(deps.as_ref(), mock_env(),
        QueryMsg::Job { job_id: 1 }).unwrap()).unwrap();
    assert_eq!(j.state, JobState::Settled);
    assert_eq!(j.guilty, None, "вердикта нет, а не «не виновен»");
    // Дважды не закрыть.
    let err = execute(deps.as_mut(), env_after(CHECK_TTL + 2), mock_info("anyone", &[]),
        ExecuteMsg::ExpireChecks { job_id: 1 }).unwrap_err();
    assert!(matches!(err, ContractError::WrongJobState { .. }), "вышло {err:?}");
}

#[test]
fn srokom_nelzya_uvesti_zadanie_ot_gotovogo_verdikta() {
    // Проверок хватает — закрывать надо Finalize. Иначе сроком можно
    // было бы увести задание от вердикта: дождаться часа и вернуть
    // деньги заказчику, хотя виновность уже доказуема.
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    submit_checks_mismatching(deps.as_mut(), id, CHECKS as usize);
    let err = execute(deps.as_mut(), env_after(CHECK_TTL + 1), mock_info("anyone", &[]),
        ExecuteMsg::ExpireChecks { job_id: id }).unwrap_err();
    assert!(matches!(err, ContractError::ChecksEnough { got: 2, need: 2 }),
            "вышло {err:?}");
    // А вердикт выносится как обычно.
    execute(deps.as_mut(), env_after(CHECK_TTL + 2), mock_info("anyone", &[]),
        ExecuteMsg::Finalize { job_id: id }).unwrap();
}

#[test]
fn nulevoy_srok_ne_prinimaetsya() {
    let mut deps = mock_dependencies();
    setup_native(deps.as_mut());
    for (c, k) in [(Some(0u64), None), (None, Some(0u64))] {
        let err = execute(deps.as_mut(), mock_env(), mock_info(ADMIN, &[]),
            ExecuteMsg::UpdateConfig {
                admin: None, checks_required: None,
                pinned_arch: None, kernel_cache_hash: None,
                pinned_model: None, pinned_runtime: None, pinned_dtype: None,
                conformance_limit_micro: None, conformance_ref: None,
                claim_ttl_seconds: c, check_ttl_seconds: k,
                settlement: None, stake_denom: None, min_stake: None,
                verifier_fee_bps: None, forfeit: None,
            }).unwrap_err();
        assert!(matches!(err, ContractError::ZeroTtl), "вышло {err:?}");
    }
}

// --- адреса доставки и список заданий --------------------------------------
//
// Добавлено 27 октября, одной правкой. Закрывалась дыра живости: связку
// исполнителя и текст запроса заказчика брали по адресам, которые
// передавались ЛЮДЬМИ, — реестра адресов не было. На правильность это не
// влияло (подмену ловит хэш), на живость влияло прямо: кто не отдал
// связку, тому не проверят ответ.
//
// Заодно появился запрос списка заданий: без него узел перебирал номера
// по одному и останавливался на отсутствующем.
//
// Главное свойство, которое проверяется ниже: адрес в цепочке НЕ даёт
// доверия. Контракт смотрит только форму, а что считать верным — решает
// хэш.

/// Адрес доставки узла, как его отдаёт запрос.
fn endpoint_of(deps: Deps, who: &str) -> Option<String> {
    let e: Executor = from_json(query(deps, mock_env(),
        QueryMsg::Executor { address: who.into() }).unwrap()).unwrap();
    e.endpoint
}

/// Страница списка заданий.
fn jobs_page(deps: Deps, start_after: Option<u64>, limit: Option<u32>)
    -> JobsResponse {
    from_json(query(deps, mock_env(),
        QueryMsg::Jobs { start_after, limit }).unwrap()).unwrap()
}

#[test]
fn uzel_bez_adresa_zadaniy_ne_beret() {
    // Залог внесён, проба пройдена, связку взять негде. Такой узел сдал
    // бы ответ, которого никто не проверит: задание дошло бы до Answered
    // и простояло час до истечения срока, заперев деньги заказчика.
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    let mut d = deps.as_mut();
    execute(d.branch(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::RegisterExecutor {
            model: MODEL.into(), runtime: RUNTIME.into(), dtype: DTYPE.into(),
            card_class: ARCH.into(), kernel_cache_hash: CACHE.into(),
        }).unwrap();
    execute(d.branch(), mock_env(), mock_info(EXEC, &coins(MIN_STAKE, STAKE)),
        ExecuteMsg::DepositStake {}).unwrap();
    pass_probe(d.branch(), EXEC, 0);
    submit_job(d.branch());

    let err = execute(d.branch(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::ClaimJob { job_id: 1 }).unwrap_err();
    assert!(matches!(err, ContractError::NoEndpoint), "вышло {err:?}");

    // Объявил адрес — берёт.
    announce(d.branch(), EXEC, ENDPOINT);
    execute(d.branch(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::ClaimJob { job_id: 1 }).unwrap();
}

#[test]
fn proveryayushchemu_adres_ne_nuzhen() {
    // Связка нужна ОТ исполнителя, а не от проверяющего: его вектор
    // раскрывается на цепочке при споре, а не забирается по http. Значит
    // требовать адрес от проверяющего нечем — и не требуется.
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    // Проверяющие заводятся через register_and_probe, то есть БЕЗ адреса.
    submit_checks_matching(deps.as_mut(), id, CHECKS as usize);
    let e: Executor = from_json(query(deps.as_ref(), mock_env(),
        QueryMsg::Executor { address: "checker0".into() }).unwrap()).unwrap();
    assert!(e.endpoint.is_none(), "проверяющий работал без адреса");
    execute(deps.as_mut(), mock_env(), mock_info("anyone", &[]),
        ExecuteMsg::Finalize { job_id: id }).unwrap();
}

#[test]
fn adres_menyaetsya_i_snimaetsya() {
    // Адрес обязан меняться: карта переарендована, машина переехала,
    // порт другой. Зарегистрироваться второй раз нельзя, поэтому способ
    // сменить адрес — отдельное сообщение.
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    assert_eq!(endpoint_of(deps.as_ref(), EXEC).as_deref(), Some(ENDPOINT));

    let other = "https://node1.example/bundles";
    announce(deps.as_mut(), EXEC, other);
    assert_eq!(endpoint_of(deps.as_ref(), EXEC).as_deref(), Some(other));

    // Снятие — законный режим: узел остаётся проверяющим, но заданий не
    // берёт.
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::SetEndpoint { endpoint: None }).unwrap();
    assert_eq!(endpoint_of(deps.as_ref(), EXEC), None);
    submit_job(deps.as_mut());
    let err = execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::ClaimJob { job_id: 1 }).unwrap_err();
    assert!(matches!(err, ContractError::NoEndpoint), "вышло {err:?}");
}

#[test]
fn adres_nezaregistrirovannogo_ne_prinimaetsya() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    let err = execute(deps.as_mut(), mock_env(), mock_info("nobody", &[]),
        ExecuteMsg::SetEndpoint { endpoint: Some(ENDPOINT.into()) }).unwrap_err();
    assert!(matches!(err, ContractError::ExecutorUnknown), "вышло {err:?}");
}

/// Адреса, которые приняты быть не должны, и почему каждый.
fn bad_uris() -> Vec<String> {
    vec![
        // Без схемы: клиент не знает, чем идти.
        "node1.example:8081".to_string(),
        // Не та схема: доставка идёт по http(s).
        "ftp://node1.example".to_string(),
        // Пусто после схемы: идти некуда.
        "http://".to_string(),
        // Пробел внутри склеится в другой запрос.
        "http://node1.example /b".to_string(),
        // Перевод строки — то же, но хуже: его не видно.
        "http://node1.example\n/b".to_string(),
        // Длиннее предела.
        format!("http://{}", "a".repeat(300)),
        // Пустая строка.
        String::new(),
    ]
}

#[test]
fn krivoy_adres_uzla_otvergaetsya() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    for uri in bad_uris() {
        let err = execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
            ExecuteMsg::SetEndpoint { endpoint: Some(uri.clone()) }).unwrap_err();
        assert!(matches!(err, ContractError::BadUri { .. }),
            "адрес {uri:?} принят, а не должен: {err:?}");
    }
    // Прежний адрес при этом цел: отказ ничего не записал.
    assert_eq!(endpoint_of(deps.as_ref(), EXEC).as_deref(), Some(ENDPOINT));
}

#[test]
fn zadanie_bez_adresa_teksta_ne_stavitsya() {
    // Задание, текст которого взять неоткуда, не выполнит никто: оно
    // простоит до отмены, заняв деньги заказчика ни за что.
    let mut deps = mock_dependencies();
    setup_native(deps.as_mut());
    for uri in bad_uris() {
        let err = execute(deps.as_mut(), mock_env(),
            mock_info(CLIENT, &coins(TOTAL, NATIVE)),
            ExecuteMsg::SubmitJob {
                prompt_hash: "ph".into(),
                prompt_uri: uri.clone(),
                price: Uint128::new(PRICE),
            }).unwrap_err();
        assert!(matches!(err, ContractError::BadUri { .. }),
            "адрес {uri:?} принят, а не должен: {err:?}");
    }
}

#[test]
fn adres_teksta_lezhit_v_zadanii() {
    let mut deps = mock_dependencies();
    setup_native(deps.as_mut());
    execute(deps.as_mut(), mock_env(), mock_info(CLIENT, &coins(TOTAL, NATIVE)),
        ExecuteMsg::SubmitJob {
            prompt_hash: "ph".into(),
            prompt_uri: PROMPT_URI.into(),
            price: Uint128::new(PRICE),
        }).unwrap();
    let job: Job = from_json(query(deps.as_ref(), mock_env(),
        QueryMsg::Job { job_id: 1 }).unwrap()).unwrap();
    assert_eq!(job.prompt_uri, PROMPT_URI);
    assert_eq!(job.state, JobState::Open);
}

#[test]
fn adres_teksta_prihodit_i_cherez_cw20() {
    // Путь оплаты на требования к заданию не влияет: иначе CW20 был бы
    // дырой, через которую ставится задание без адреса текста.
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    submit_job(deps.as_mut());
    let job: Job = from_json(query(deps.as_ref(), mock_env(),
        QueryMsg::Job { job_id: 1 }).unwrap()).unwrap();
    assert_eq!(job.prompt_uri, PROMPT_URI);

    let err = execute(deps.as_mut(), mock_env(), mock_info(CW20, &[]),
        ExecuteMsg::Receive(Cw20ReceiveMsg {
            sender: CLIENT.into(),
            amount: Uint128::new(TOTAL),
            msg: to_json_binary(&ReceiveHook::SubmitJob {
                prompt_hash: "ph".into(),
                prompt_uri: "ftp://nowhere".into(),
                price: Uint128::new(PRICE),
            }).unwrap(),
        })).unwrap_err();
    assert!(matches!(err, ContractError::BadUri { .. }), "вышло {err:?}");
}

/// Поставить `n` заданий родной монетой. Номера сплошные, с единицы.
fn submit_n_native(deps: DepsMut, n: u64) {
    let mut d = deps;
    for i in 1..=n {
        execute(d.branch(), mock_env(), mock_info(CLIENT, &coins(TOTAL, NATIVE)),
            ExecuteMsg::SubmitJob {
                prompt_hash: format!("ph{i}"),
                prompt_uri: PROMPT_URI.into(),
                price: Uint128::new(PRICE),
            }).unwrap();
    }
}

#[test]
fn spisok_zadaniy_idyot_po_poryadku_i_stranicami() {
    let mut deps = mock_dependencies();
    setup_native(deps.as_mut());
    submit_n_native(deps.as_mut(), 7);

    let first = jobs_page(deps.as_ref(), None, Some(3));
    assert_eq!(first.jobs.iter().map(|e| e.job_id).collect::<Vec<_>>(),
               vec![1, 2, 3]);
    // Граница списка приходит тем же запросом: иначе цикл узла стоил бы
    // двух вместо одного.
    assert_eq!(first.next_job, 8);
    // Записи отдаются целиком, а не номерами: узлу нужны состояние,
    // хэши и адрес текста.
    assert_eq!(first.jobs[0].job.prompt_hash, "ph1");
    assert_eq!(first.jobs[0].job.prompt_uri, PROMPT_URI);

    // `start_after` значит ПОСЛЕ: продолжение не повторяет последнее
    // отданное задание.
    let next = jobs_page(deps.as_ref(), Some(3), Some(3));
    assert_eq!(next.jobs.iter().map(|e| e.job_id).collect::<Vec<_>>(),
               vec![4, 5, 6]);
    let tail = jobs_page(deps.as_ref(), Some(6), Some(3));
    assert_eq!(tail.jobs.iter().map(|e| e.job_id).collect::<Vec<_>>(), vec![7]);
    // Конец списка — пустая страница, а не ошибка: отличать «больше нет»
    // от «цепочка не ответила» узел обязан, и теперь для этого не нужно
    // разбирать текст ошибки.
    let past = jobs_page(deps.as_ref(), Some(7), Some(3));
    assert!(past.jobs.is_empty());
    assert_eq!(past.next_job, 8);
}

#[test]
fn stranica_ogranichena_sverhu() {
    // Предел нужен не из вежливости: запись задания это три хэша, два
    // адреса и адрес текста, и страница в тысячу вернулась бы отказом
    // узла REST — то есть выглядела бы недоступностью цепочки.
    let mut deps = mock_dependencies();
    setup_native(deps.as_mut());
    submit_n_native(deps.as_mut(), 60);
    assert_eq!(jobs_page(deps.as_ref(), None, Some(1000)).jobs.len(), 50);
    // Без limit — умолчание.
    assert_eq!(jobs_page(deps.as_ref(), None, None).jobs.len(), 30);
    assert_eq!(jobs_page(deps.as_ref(), None, Some(0)).jobs.len(), 0);
}

#[test]
fn granica_spiska_est_i_do_pervogo_zadaniya() {
    let mut deps = mock_dependencies();
    setup_native(deps.as_mut());
    let r: NextJobResponse = from_json(query(deps.as_ref(), mock_env(),
        QueryMsg::NextJob {}).unwrap()).unwrap();
    assert_eq!(r.next_job, 1, "следующее задание — первое");
    let page = jobs_page(deps.as_ref(), None, None);
    assert!(page.jobs.is_empty());
    assert_eq!(page.next_job, 1);
}

#[test]
fn zakrytoe_zadanie_iz_spiska_ne_propadaet() {
    // Номера сплошные, и это свойство, на которое опирается узел:
    // отмена задания его не удаляет, а закрывает.
    let mut deps = mock_dependencies();
    setup_native(deps.as_mut());
    submit_n_native(deps.as_mut(), 3);
    execute(deps.as_mut(), mock_env(), mock_info(CLIENT, &[]),
        ExecuteMsg::CancelJob { job_id: 2 }).unwrap();
    let page = jobs_page(deps.as_ref(), None, None);
    assert_eq!(page.jobs.iter().map(|e| e.job_id).collect::<Vec<_>>(),
               vec![1, 2, 3]);
    assert_eq!(page.jobs[1].job.state, JobState::Settled);
}

// --- перенос формы 3 на форму 4 -------------------------------------------

/// Сырой ключ записи в карте cw-storage-plus: двухбайтовая длина
/// пространства имён, само пространство, затем ключ.
///
/// Нужен затем, чтобы положить в хранилище запись ПРЕЖНЕЙ формы — без
/// новых полей. Иначе перенос проверялся бы на записях, которые сам же
/// код и создал, то есть на том, чего на живой цепочке нет.
fn raw_map_key(ns: &[u8], key: &[u8]) -> Vec<u8> {
    let mut k = (ns.len() as u16).to_be_bytes().to_vec();
    k.extend_from_slice(ns);
    k.extend_from_slice(key);
    k
}

#[test]
fn perenos_formy_3_dobavlyaet_adresa() {
    // Это тот перенос, который пойдёт на живую цепочку: все три
    // экземпляра на форме 3 (code_id 341, проверено 27 октября).
    //
    // Настройки при нём НЕ меняются — меняются записи узлов и заданий, у
    // которых появились адреса. Поэтому главное здесь: записи ПРЕЖНЕЙ
    // формы читаются после переноса, а не отказывают разбором.
    let mut deps = mock_dependencies();
    setup_native(deps.as_mut());
    let before: Config = from_json(
        query(deps.as_ref(), mock_env(), QueryMsg::Config {}).unwrap()).unwrap();

    // Запись узла прежней формы: все поля формы 3, адреса нет.
    let old_exec = serde_json::json!({
        "model": MODEL, "runtime": RUNTIME, "dtype": DTYPE,
        "card_class": ARCH, "kernel_cache_hash": CACHE,
        "status": "active", "jobs_done": 0, "convictions": 0, "abandoned": 0,
        "stake": MIN_STAKE.to_string(),
        "conformance": {
            "divergence_micro": 0,
            "logprobs_hash": commit_hash(&probe_honest()),
            "ref_version": 1,
        },
    });
    deps.as_mut().storage.set(
        &raw_map_key(b"executors", EXEC.as_bytes()),
        serde_json::to_vec(&old_exec).unwrap().as_slice());

    // Задание прежней формы: адреса текста нет.
    let old_job = serde_json::json!({
        "client": CLIENT, "prompt_hash": "ph", "executor": null,
        "state": "open", "answer_hash": null, "logprobs_hash": null,
        "answer_tokens": null, "guilty": null, "reveal_verified": null,
        "price": PRICE.to_string(), "fee_total": FEE.to_string(),
        "paid": false, "claimed_at": null, "answered_at": null,
    });
    deps.as_mut().storage.set(
        &raw_map_key(b"jobs", &1u64.to_be_bytes()),
        serde_json::to_vec(&old_job).unwrap().as_slice());
    STATE_VERSION_FOR_TEST.save(deps.as_mut().storage, &3u16).unwrap();

    let res = migrate(deps.as_mut(), mock_env(), MigrateMsg {}).unwrap();
    assert!(res.attributes.iter().any(|a| a.key == "from" && a.value == "3"));
    assert!(res.attributes.iter().any(|a| a.key == "changed"
        && a.value == "endpoint,prompt_uri,jobs_query"));
    assert_eq!(STATE_VERSION_FOR_TEST.load(deps.as_ref().storage).unwrap(),
               CURRENT_VERSION);

    // Настройки не тронуты: в форме 4 они те же.
    let after: Config = from_json(
        query(deps.as_ref(), mock_env(), QueryMsg::Config {}).unwrap()).unwrap();
    assert_eq!(before, after, "перенос настроек не менял");

    // Запись прежней формы ЧИТАЕТСЯ, и адреса у неё нет.
    let e: Executor = from_json(query(deps.as_ref(), mock_env(),
        QueryMsg::Executor { address: EXEC.into() }).unwrap()).unwrap();
    assert_eq!(e.endpoint, None);
    assert_eq!(e.stake, Uint128::new(MIN_STAKE));
    assert!(e.conformance.is_some(), "проба цела");
    let j: Job = from_json(query(deps.as_ref(), mock_env(),
        QueryMsg::Job { job_id: 1 }).unwrap()).unwrap();
    assert_eq!(j.prompt_uri, "", "адреса текста у прежней записи нет");
    assert_eq!(j.prompt_hash, "ph");

    // И узел прежней формы не берёт задания, пока не объявит адрес, —
    // отказом, а не молча.
    let err = execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::ClaimJob { job_id: 1 }).unwrap_err();
    assert!(matches!(err, ContractError::NoEndpoint), "вышло {err:?}");
    announce(deps.as_mut(), EXEC, ENDPOINT);
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::ClaimJob { job_id: 1 }).unwrap();
}

// --- залог: внести, донести, забрать ---------------------------------------
//
// Добавлено 28 октября, когда писалась инструкция владельцу карты. Писать
// её про частичный возврат было нечего: `Pending` оказался дорогой в одну
// сторону. Узел, забравший хоть единицу ниже минимума, становился
// непригоден НАВСЕГДА — `Active` ставилось только при регистрации, а
// зарегистрироваться второй раз нельзя. Деньги не пропадали, но адрес
// приходилось бросать: новый залог, новая проба.
//
// Ниже по тесту на каждое свойство возврата, потому что инструкция
// обещает их человеку, который тратит деньги.

/// Статус узла, как его отдаёт запрос.
fn status_of(deps: Deps, who: &str) -> ExecutorStatus {
    let e: Executor = from_json(query(deps, mock_env(),
        QueryMsg::Executor { address: who.into() }).unwrap()).unwrap();
    e.status
}

fn stake_of(deps: Deps, who: &str) -> Uint128 {
    let e: Executor = from_json(query(deps, mock_env(),
        QueryMsg::Executor { address: who.into() }).unwrap()).unwrap();
    e.stake
}

#[test]
fn chastichnyy_vozvrat_obratim() {
    // Главное свойство: забрал ниже минимума — не работает, донёс —
    // снова работает. Без обратимости частичный возврат был бы ловушкой,
    // и инструкции пришлось бы запрещать то, что контракт разрешает.
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    assert_eq!(status_of(deps.as_ref(), EXEC), ExecutorStatus::Active);

    // Забираем ОДНУ единицу: этого хватает, чтобы выпасть из минимума.
    let res = execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::WithdrawStake { amount: Uint128::one() }).unwrap();
    assert_eq!(res.messages.len(), 1, "деньги уходят одним сообщением");
    assert_eq!(status_of(deps.as_ref(), EXEC), ExecutorStatus::Pending);
    assert_eq!(stake_of(deps.as_ref(), EXEC), Uint128::new(MIN_STAKE - 1));

    // Задания не дают, и отказ говорит про статус, а не про подлог.
    submit_job(deps.as_mut());
    let err = execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::ClaimJob { job_id: 1 }).unwrap_err();
    assert!(matches!(err, ContractError::ExecutorNotActive { ref status }
        if status == "Pending"), "вышло {err:?}");

    // Донёс ровно недостающее — снова допущен, и это видно в событии.
    let res = execute(deps.as_mut(), mock_env(),
        mock_info(EXEC, &coins(1, STAKE)),
        ExecuteMsg::DepositStake {}).unwrap();
    assert!(res.attributes.iter().any(|a| a.key == "status"
        && a.value == "Active"), "событие не назвало статус");
    assert_eq!(status_of(deps.as_ref(), EXEC), ExecutorStatus::Active);
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::ClaimJob { job_id: 1 }).unwrap();
}

#[test]
fn nedonesyonnyy_zalog_ne_vozvrashchaet_dopusk() {
    // Донёс, но не до минимума — остаётся Pending. Иначе «донести»
    // значило бы «положить сколько угодно».
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::WithdrawStake { amount: Uint128::new(1000) }).unwrap();
    assert_eq!(status_of(deps.as_ref(), EXEC), ExecutorStatus::Pending);
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &coins(999, STAKE)),
        ExecuteMsg::DepositStake {}).unwrap();
    assert_eq!(status_of(deps.as_ref(), EXEC), ExecutorStatus::Pending,
               "залога всё ещё не хватает");
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &coins(1, STAKE)),
        ExecuteMsg::DepositStake {}).unwrap();
    assert_eq!(status_of(deps.as_ref(), EXEC), ExecutorStatus::Active);
}

#[test]
fn vznos_zabanennogo_statusa_ne_vozvrashchaet() {
    // Обратимость — только для Pending. Banned необратим, и взнос от
    // него не принимается вовсе: иначе монеты заперло бы навсегда.
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    submit_checks_mismatching(deps.as_mut(), id, CHECKS as usize);
    execute(deps.as_mut(), mock_env(), mock_info("anyone", &[]),
        ExecuteMsg::Finalize { job_id: id }).unwrap();
    assert_eq!(status_of(deps.as_ref(), EXEC), ExecutorStatus::Banned);
    assert_eq!(stake_of(deps.as_ref(), EXEC), Uint128::zero(),
               "залог изъят целиком");

    let err = execute(deps.as_mut(), mock_env(),
        mock_info(EXEC, &coins(MIN_STAKE, STAKE)),
        ExecuteMsg::DepositStake {}).unwrap_err();
    assert!(matches!(err, ContractError::ExecutorBanned), "вышло {err:?}");
    let err = execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::WithdrawStake { amount: Uint128::one() }).unwrap_err();
    assert!(matches!(err, ContractError::ExecutorBanned), "вышло {err:?}");
}

#[test]
fn vozvrat_celikom_otdayot_vsyo_i_tolko_svoyo() {
    // Уходя, узел забирает ровно внесённое — ни больше, ни меньше, — и
    // сумма уходит в единице залога, а не в единице расчёта.
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let res = execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::WithdrawStake { amount: Uint128::new(MIN_STAKE) }).unwrap();
    assert_eq!(stake_of(deps.as_ref(), EXEC), Uint128::zero());
    match &res.messages[0].msg {
        cosmwasm_std::CosmosMsg::Bank(cosmwasm_std::BankMsg::Send {
            to_address, amount }) => {
            assert_eq!(to_address, EXEC, "деньги уходят самому узлу");
            assert_eq!(amount.len(), 1);
            assert_eq!(amount[0].denom, STAKE, "единица залога, не расчёта");
            assert_eq!(amount[0].amount, Uint128::new(MIN_STAKE));
        }
        other => panic!("не перевод залога: {other:?}"),
    }
    // Больше внесённого не отдаётся.
    let err = execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::WithdrawStake { amount: Uint128::one() }).unwrap_err();
    assert!(matches!(err, ContractError::StakeInsufficient { .. }),
            "вышло {err:?}");
}

#[test]
fn zalog_otpuskaetsya_posle_verdikta_i_posle_sroka() {
    // Незакрытое задание держит залог. Но держит НЕ НАВСЕГДА: вердикт
    // его отпускает, и истёкший срок — тоже, а закрыть просроченное
    // вправе кто угодно, в том числе сам узел.
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    let err = execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::WithdrawStake { amount: Uint128::one() }).unwrap_err();
    assert!(matches!(err, ContractError::StakeLocked { open: 1 }),
            "вышло {err:?}");
    submit_checks_matching(deps.as_mut(), id, CHECKS as usize);
    execute(deps.as_mut(), mock_env(), mock_info("anyone", &[]),
        ExecuteMsg::Finalize { job_id: id }).unwrap();
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::WithdrawStake { amount: Uint128::new(MIN_STAKE) }).unwrap();
    assert_eq!(stake_of(deps.as_ref(), EXEC), Uint128::zero(),
               "после вердикта залог свободен");

    // Второй случай: задание взято и брошено. Срок вышел, задание
    // освобождает САМ узел, и залог отпускает.
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &coins(MIN_STAKE, STAKE)),
        ExecuteMsg::DepositStake {}).unwrap();
    submit_job(deps.as_mut());
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::ClaimJob { job_id: 2 }).unwrap();
    let err = execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::WithdrawStake { amount: Uint128::one() }).unwrap_err();
    assert!(matches!(err, ContractError::StakeLocked { open: 1 }),
            "вышло {err:?}");
    execute(deps.as_mut(), env_after(CLAIM_TTL + 1), mock_info(EXEC, &[]),
        ExecuteMsg::ReleaseJob { job_id: 2 }).unwrap();
    execute(deps.as_mut(), env_after(CLAIM_TTL + 1), mock_info(EXEC, &[]),
        ExecuteMsg::WithdrawStake { amount: Uint128::new(MIN_STAKE) }).unwrap();
    assert_eq!(stake_of(deps.as_ref(), EXEC), Uint128::zero(),
               "брошенное задание залог не держит после срока");
    // И это не наказание: бана нет, только счётчик.
    let e: Executor = from_json(query(deps.as_ref(), mock_env(),
        QueryMsg::Executor { address: EXEC.into() }).unwrap()).unwrap();
    assert_eq!(e.abandoned, 1);
    assert_ne!(e.status, ExecutorStatus::Banned);
}

#[test]
fn proveryayushchiy_bez_zaloga_rabotaet_i_posle_vozvrata() {
    // Залог нужен исполнителю, а не проверяющему: забрав его целиком,
    // узел остаётся проверяющим. Иначе возврат залога вышибал бы из
    // сети проверки, которых и так не хватает.
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_stake(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    // Проверяющий заводится без залога вовсе — и сдаёт проверку.
    register_and_probe(deps.as_mut(), "checkerx");
    execute(deps.as_mut(), mock_env(), mock_info("checkerx", &[]),
        ExecuteMsg::SubmitCheck {
            job_id: id,
            divergence_micro: 0,
            logprobs_hash: COMMIT.into(),
        }).unwrap();
    assert_eq!(stake_of(deps.as_ref(), "checkerx"), Uint128::zero());
}

#[test]
fn testovyy_list_otlichaetsya_rovno_zalogom() {
    // Тестовый экземпляр нужен затем, что трёх узлов на настоящих картах
    // не проверял никто: залог 30 400 AXM — три такие суммы из своего
    // кармана. С маленьким залогом проверяется всё остальное: сойдутся ли
    // хэши у ТРЁХ разных карт одной модели, достанут ли узлы связки друг
    // у друга через проброшенные порты, уложится ли настоящий инференс в
    // срок.
    //
    // Но полезен он РОВНО до тех пор, пока отличается от боевого одним
    // полем. Разойдись что-то ещё — предел пробы, кеш ядер, число
    // проверок, — и прогон перестанет говорить о настоящем классе, а
    // заметит это разве что узел, получивший отказ. Поэтому сверяются не
    // отличия, а совпадения.
    let boevoy: InstantiateMsg =
        serde_json::from_str(include_str!("../deploy/instantiate.json"))
            .expect("боевой лист не разобран");
    let test: InstantiateMsg =
        serde_json::from_str(include_str!("../deploy/instantiate-test.json"))
            .expect("тестовый лист не разобран");

    // Отличие — и только оно.
    assert_eq!(boevoy.min_stake, Uint128::new(30_400_000_000));
    assert_eq!(test.min_stake, Uint128::new(100_000),
               "в тестовом листе залог 0,1 AXM");

    // Всё остальное — побайтово как в боевом.
    assert_eq!(test.admin, boevoy.admin);
    assert_eq!(test.checks_required, boevoy.checks_required);
    assert_eq!(test.pinned_model, boevoy.pinned_model);
    assert_eq!(test.pinned_runtime, boevoy.pinned_runtime);
    assert_eq!(test.pinned_dtype, boevoy.pinned_dtype);
    assert_eq!(test.pinned_arch, boevoy.pinned_arch, "тот же класс карт");
    assert_eq!(test.kernel_cache_hash, boevoy.kernel_cache_hash,
               "тот же кеш ядер: иначе карты не те");
    assert_eq!(test.conformance_limit_micro, boevoy.conformance_limit_micro,
               "предел пробы нулевой и здесь: допуск не ослабляется");
    assert_eq!(test.conformance_ref_hash, boevoy.conformance_ref_hash,
               "тот же эталон пробы");
    assert_eq!(test.conformance_lens, boevoy.conformance_lens);
    assert_eq!(test.claim_ttl_seconds, boevoy.claim_ttl_seconds);
    assert_eq!(test.check_ttl_seconds, boevoy.check_ttl_seconds);
    assert_eq!(test.stake_denom, boevoy.stake_denom);
    assert_eq!(test.verifier_fee_bps, boevoy.verifier_fee_bps);
    assert_eq!(test.settlement, boevoy.settlement);
    assert_eq!(test.forfeit, boevoy.forfeit);

    // И тестовый лист обязан разворачиваться: лист с опечаткой стоил бы
    // 0,45 AXM, выясняясь на живой цепочке.
    let mut deps = mock_dependencies();
    instantiate(deps.as_mut(), mock_env(), mock_info(ADMIN, &[]), test).unwrap();
    let cfg: Config = from_json(
        query(deps.as_ref(), mock_env(), QueryMsg::Config {}).unwrap()).unwrap();
    assert_eq!(cfg.min_stake, Uint128::new(100_000));
    assert_eq!(cfg.conformance_version, 1);
}
