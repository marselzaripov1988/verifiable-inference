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
use cosmwasm_std::{coins, from_json, to_json_binary, Addr, DepsMut, Uint128};

use crate::contract::{commit_hash, execute, instantiate, query};
use crate::error::ContractError;
use crate::msg::{
    ChecksResponse, ConformanceRef, Cw20ReceiveMsg, ExecuteMsg, InstantiateMsg, QueryMsg,
    ReceiveHook,
};
use crate::state::{Executor, ExecutorStatus, Forfeit, Job, JobState, MicroLog};

const ADMIN: &str = "admin";
const ATTESTOR: &str = "attestor";
const EXEC: &str = "executor1";
const CLIENT: &str = "client1";

const MODEL: &str = "Qwen/Qwen2.5-7B-Instruct";
const RUNTIME: &str = "vllm-0.31.0";
const DTYPE: &str = "bfloat16";
// Имена денomов нигде в логике не участвуют — только в настройках.
const SETTLEMENT: &str = "cw20:axm1uzldhz4dvv7k7gtc7jl5ahlj84eghc52p0r6zhnjca0d20dxcfkshsd8uz";
const STAKE: &str = "uaxm";
const CW20: &str = "cw20token";
const MIN_STAKE: u128 = 50_000_000;
const PRICE: u128 = 1_000_000;
const FEE_BPS: u16 = 970; // 9,7% — замеренные накладные при четырёх проверках

const THRESHOLD: u64 = 36_000;
const FRAUD_MIN: u64 = 37_780;
const HONEST_MEAN: u64 = 12_700;
const HONEST_WORST: u64 = 44_660;

// --- проба на эталонном векторе -----------------------------------------
// Предел 25_000 из замера: честный узел на ЧУЖОЙ карте даёт максимум
// 20_137, а тот же узел в неверном режиме исполнения — 413_488.
const CONF_LIMIT: u64 = 25_000;
/// Две «пробы» по три числа: больше для проверки правила не нужно, а
/// границы текстов обязаны быть именно двумя, иначе максимум по текстам
/// не отличить от среднего по склейке.
const PROBE_LENS: [u32; 2] = [3, 3];
fn probe_ref() -> Vec<MicroLog> {
    vec![-500_000, -500_000, -500_000, -400_000, -400_000, -400_000]
}
/// Узел, сошедшийся с эталоном: ровно честное среднее на каждом элементе.
fn probe_honest() -> Vec<MicroLog> {
    probe_ref()
        .iter()
        .map(|x| x - HONEST_MEAN as i64)
        .collect()
}

fn setup(deps: DepsMut) {
    instantiate(
        deps,
        mock_env(),
        mock_info(ADMIN, &[]),
        InstantiateMsg {
            admin: ADMIN.into(),
            attestor: ATTESTOR.into(),
            threshold_micro: THRESHOLD,
            checks_required: 4,
            min_answer_tokens: 32,
            pinned_model: MODEL.into(),
            pinned_runtime: RUNTIME.into(),
            pinned_dtype: DTYPE.into(),
            conformance_limit_micro: CONF_LIMIT,
            conformance_ref_hash: commit_hash(&probe_ref()),
            conformance_lens: PROBE_LENS.to_vec(),
            settlement_denom: SETTLEMENT.into(),
            stake_denom: STAKE.into(),
            settlement_cw20: CW20.into(),
            min_stake: Uint128::new(MIN_STAKE),
            verifier_fee_bps: FEE_BPS,
            forfeit: Forfeit::Burn,
        },
    )
    .unwrap();
}

fn register_and_attest(deps: DepsMut, who: &str) {
    let mut d = deps;
    execute(
        d.branch(),
        mock_env(),
        mock_info(who, &[]),
        ExecuteMsg::RegisterExecutor {
            model: MODEL.into(),
            runtime: RUNTIME.into(),
            dtype: DTYPE.into(),
            card_class: "rtx4090".into(),
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
    execute(
        d.branch(),
        mock_env(),
        mock_info(ATTESTOR, &[]),
        ExecuteMsg::Attest {
            executor: who.into(),
            eligible: true,
        },
    )
    .unwrap();
    pass_probe(d.branch(), who, HONEST_MEAN);
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
            card_class: "rtx4090".into(),
        },
    )
    .unwrap();
    pass_probe(d.branch(), who, HONEST_MEAN);
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
            amount: Uint128::new(PRICE),
            msg: to_json_binary(&ReceiveHook::SubmitJob {
                prompt_hash: "ph".into(),
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

fn submit_checks(deps: DepsMut, job_id: u64, values: &[u64]) {
    let mut d = deps;
    for (i, v) in values.iter().enumerate() {
        let who = format!("checker{}", i);
        // Проверяющий тоже обязан пройти пробу: сравниваются его числа.
        register_and_probe(d.branch(), &who);
        execute(
            d.branch(),
            mock_env(),
            mock_info(&format!("checker{}", i), &[]),
            ExecuteMsg::SubmitCheck {
                job_id,
                divergence_micro: *v,
                logprobs_hash: format!("hash{}", i),
            },
        )
        .unwrap();
    }
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
            card_class: "rtx4090".into(),
        },
    )
    .unwrap_err();
    assert!(matches!(err, ContractError::WrongPinning { .. }));
}

#[test]
fn bez_dopuska_zadanie_ne_vzyat() {
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
            card_class: "rtx3090".into(),
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
    assert_eq!(e.status, ExecutorStatus::Pending);

    submit_job(deps.as_mut());
    let err = execute(
        deps.as_mut(),
        mock_env(),
        mock_info(EXEC, &[]),
        ExecuteMsg::ClaimJob { job_id: 1 },
    )
    .unwrap_err();
    assert!(matches!(err, ContractError::ExecutorNotActive { .. }));
}

#[test]
fn korotkiy_otvet_otklonyaetsya() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_attest(deps.as_mut(), EXEC);
    submit_job(deps.as_mut());
    execute(
        deps.as_mut(),
        mock_env(),
        mock_info(EXEC, &[]),
        ExecuteMsg::ClaimJob { job_id: 1 },
    )
    .unwrap();
    // 31 токен: ниже границы 32, где запас падает до 1,6x
    let err = execute(
        deps.as_mut(),
        mock_env(),
        mock_info(EXEC, &[]),
        ExecuteMsg::SubmitAnswer {
            job_id: 1,
            answer_hash: "ah".into(),
            logprobs_hash: "lh".into(),
            answer_tokens: 31,
        },
    )
    .unwrap_err();
    assert!(matches!(
        err,
        ContractError::AnswerTooShort { got: 31, need: 32 }
    ));
}

#[test]
fn ispolnitel_ne_proveryaet_sebya() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_attest(deps.as_mut(), EXEC);
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
    register_and_attest(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    submit_checks(deps.as_mut(), id, &[HONEST_MEAN]);
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
    register_and_attest(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    submit_checks(deps.as_mut(), id, &[FRAUD_MIN, FRAUD_MIN, FRAUD_MIN]);
    let err = execute(
        deps.as_mut(),
        mock_env(),
        mock_info("anyone", &[]),
        ExecuteMsg::Finalize { job_id: id },
    )
    .unwrap_err();
    assert!(matches!(
        err,
        ContractError::NotEnoughChecks { got: 3, need: 4 }
    ));
}

#[test]
fn podlog_na_samom_nezametnom_lovitsya() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_attest(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    // Худший случай: минимум измеренного подлога, 1 780 микродолей над порогом
    submit_checks(
        deps.as_mut(),
        id,
        &[FRAUD_MIN, FRAUD_MIN + 920, FRAUD_MIN + 1960, FRAUD_MIN + 6430],
    );
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
    register_and_attest(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    // Три превышения и одно нет — правило «все превысили» не срабатывает.
    // Это и есть защита от сговора трёх проверяющих.
    submit_checks(
        deps.as_mut(),
        id,
        &[FRAUD_MIN, FRAUD_MIN, FRAUD_MIN, HONEST_MEAN],
    );
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
    register_and_attest(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    // 44 660 — худшее честное наблюдение из 1520, оно ВЫШЕ порога.
    // Но чтобы обвинить, таких нужно четыре подряд, а их доля 0,26%:
    // здесь три честных типичных и одно худшее.
    submit_checks(
        deps.as_mut(),
        id,
        &[HONEST_WORST, HONEST_MEAN, HONEST_MEAN, HONEST_MEAN],
    );
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
fn na_poroge_rovno_eto_prevyshenie() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_attest(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    // Граница включающая: >= порога. Фиксируем это тестом, чтобы никто
    // не поменял на > и не сдвинул правило молча.
    submit_checks(deps.as_mut(), id, &[THRESHOLD; 4]);
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
    register_and_attest(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    submit_checks(deps.as_mut(), id, &[HONEST_MEAN; 4]);
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
    register_and_attest(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    submit_checks(deps.as_mut(), id, &[FRAUD_MIN; 4]);
    execute(
        deps.as_mut(),
        mock_env(),
        mock_info("anyone", &[]),
        ExecuteMsg::Finalize { job_id: id },
    )
    .unwrap();
    // Ни attestor, ни админ не возвращают пойманного: иначе наказание
    // ничего не стоит.
    let err = execute(
        deps.as_mut(),
        mock_env(),
        mock_info(ATTESTOR, &[]),
        ExecuteMsg::Attest {
            executor: EXEC.into(),
            eligible: true,
        },
    )
    .unwrap_err();
    assert!(matches!(err, ContractError::ExecutorBanned));
    let err = execute(
        deps.as_mut(),
        mock_env(),
        mock_info(ADMIN, &[]),
        ExecuteMsg::SetSuspended {
            executor: EXEC.into(),
            suspended: false,
        },
    )
    .unwrap_err();
    assert!(matches!(err, ContractError::ExecutorBanned));
}

#[test]
fn prava_razdeleny() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    // attestor не меняет настройки
    let err = execute(
        deps.as_mut(),
        mock_env(),
        mock_info(ATTESTOR, &[]),
        ExecuteMsg::UpdateConfig {
            attestor: None,
            threshold_micro: Some(1),
            checks_required: None,
            min_answer_tokens: None,
            pinned_model: None,
            pinned_runtime: None,
            pinned_dtype: None,
            conformance_limit_micro: None,
            conformance_ref: None,
            settlement_denom: None,
            stake_denom: None,
            min_stake: None,
            verifier_fee_bps: None,
            forfeit: None,
        },
    )
    .unwrap_err();
    assert!(matches!(err, ContractError::NotAdmin));
    // админ не подтверждает допуск
    execute(
        deps.as_mut(),
        mock_env(),
        mock_info(EXEC, &[]),
        ExecuteMsg::RegisterExecutor {
            model: MODEL.into(),
            runtime: RUNTIME.into(),
            dtype: DTYPE.into(),
            card_class: "a100".into(),
        },
    )
    .unwrap();
    let err = execute(
        deps.as_mut(),
        mock_env(),
        mock_info(ADMIN, &[]),
        ExecuteMsg::Attest {
            executor: EXEC.into(),
            eligible: true,
        },
    )
    .unwrap_err();
    assert!(matches!(err, ContractError::NotAttestor));
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
            attestor: ATTESTOR.into(),
            threshold_micro: THRESHOLD,
            checks_required: 0,
            min_answer_tokens: 32,
            pinned_model: MODEL.into(),
            pinned_runtime: RUNTIME.into(),
            pinned_dtype: DTYPE.into(),
            conformance_limit_micro: CONF_LIMIT,
            conformance_ref_hash: commit_hash(&probe_ref()),
            conformance_lens: PROBE_LENS.to_vec(),
            settlement_denom: SETTLEMENT.into(),
            stake_denom: STAKE.into(),
            settlement_cw20: CW20.into(),
            min_stake: Uint128::new(MIN_STAKE),
            verifier_fee_bps: FEE_BPS,
            forfeit: Forfeit::Burn,
        },
    )
    .unwrap_err();
    assert!(matches!(err, ContractError::ZeroChecks));
}

#[test]
fn zapros_proverok_schitaet_prevysheniya() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_attest(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    submit_checks(
        deps.as_mut(),
        id,
        &[FRAUD_MIN, HONEST_MEAN, FRAUD_MIN, HONEST_MEAN],
    );
    let r: ChecksResponse = from_json(
        query(deps.as_ref(), mock_env(), QueryMsg::Checks { job_id: id }).unwrap(),
    )
    .unwrap();
    assert_eq!(r.checks.len(), 4);
    assert_eq!(r.exceeded, 2);
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
    register_and_attest(deps.as_mut(), EXEC);
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
    register_and_attest(deps.as_mut(), EXEC);
    let prod: Vec<i64> = (0..100).map(|i| -500_000 - i * 1000).collect();
    let chk: Vec<i64> = prod.iter().map(|x| x - 12_700).collect();
    let id = job_with_commit(deps.as_mut(), &prod);
    // Заявил подлог там, где его нет: обязался честным вектором, а число
    // назвал выше порога. Это и есть ложное обвинение.
    register_and_probe(deps.as_mut(), "checker0");
    execute(deps.as_mut(), mock_env(), mock_info("checker0", &[]),
        ExecuteMsg::SubmitCheck {
            job_id: id,
            divergence_micro: FRAUD_MIN,
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
    register_and_attest(deps.as_mut(), EXEC);
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
    register_and_attest(deps.as_mut(), EXEC);
    let prod: Vec<i64> = (0..100).map(|i| -500_000 - i * 1000).collect();
    let chk: Vec<i64> = prod.iter().map(|x| x - 12_700).collect();
    let id = job_with_commit(deps.as_mut(), &prod);
    for i in 0..4 {
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
    // Зарегистрировался и допущен, но залога не внёс. Проверка залога
    // стоит при ВЗЯТИИ задания, а не только при допуске.
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::RegisterExecutor {
            model: MODEL.into(), runtime: RUNTIME.into(),
            dtype: DTYPE.into(), card_class: "rtx4090".into(),
        }).unwrap();
    execute(deps.as_mut(), mock_env(), mock_info(ATTESTOR, &[]),
        ExecuteMsg::Attest { executor: EXEC.into(), eligible: true }).unwrap();
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
            amount: Uint128::new(PRICE),
            msg: to_json_binary(&ReceiveHook::SubmitJob {
                prompt_hash: "ph".into() }).unwrap(),
        })).unwrap_err();
    assert!(matches!(err, ContractError::WrongCw20 { .. }));
}

#[test]
fn zalog_derzhitsya_nezakrytym_zadaniem() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_attest(deps.as_mut(), EXEC);
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
    register_and_attest(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    submit_checks(deps.as_mut(), id, &[HONEST_MEAN; 4]);
    let res = execute(deps.as_mut(), mock_env(), mock_info("anyone", &[]),
        ExecuteMsg::Finalize { job_id: id }).unwrap();
    // 970 bps от 1 000 000 это 97 000, на четверых по 24 250.
    let paid: u128 = (97_000u128 / 4) * 4;
    assert!(res.attributes.iter().any(|a| a.key == "verifier_fees"
        && a.value == paid.to_string()));
    assert!(res.attributes.iter().any(|a| a.key == "settled"
        && a.value == (PRICE - paid).to_string()));
    // Четыре сообщения проверяющим плюс одно исполнителю.
    assert_eq!(res.messages.len(), 5);
}

#[test]
fn vinovnomu_ne_platyat_zalog_izymayut() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_attest(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    submit_checks(deps.as_mut(), id, &[FRAUD_MIN; 4]);
    let res = execute(deps.as_mut(), mock_env(), mock_info("anyone", &[]),
        ExecuteMsg::Finalize { job_id: id }).unwrap();
    assert!(res.attributes.iter().any(|a| a.key == "guilty" && a.value == "true"));
    // Четыре проверяющим, возврат заказчику, изъятие залога.
    assert_eq!(res.messages.len(), 6);
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
    for values in [[HONEST_MEAN; 4], [FRAUD_MIN; 4]] {
        let mut deps = mock_dependencies();
        setup(deps.as_mut());
        register_and_attest(deps.as_mut(), EXEC);
        let id = job_until_answered(deps.as_mut());
        submit_checks(deps.as_mut(), id, &values);
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
            attestor: None, threshold_micro: None, checks_required: None,
            min_answer_tokens: None, pinned_model: None, pinned_runtime: None,
            pinned_dtype: None, conformance_limit_micro: None, conformance_ref: None,
            settlement_denom: None, stake_denom: None,
            min_stake: None, verifier_fee_bps: None,
            forfeit: Some(Forfeit::ToAddress(Addr::unchecked("community_pool"))),
        }).unwrap();
    register_and_attest(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    submit_checks(deps.as_mut(), id, &[FRAUD_MIN; 4]);
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
    register_and_attest(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    submit_checks(deps.as_mut(), id, &[HONEST_MEAN; 4]);
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
            attestor: None, threshold_micro: None, checks_required: None,
            min_answer_tokens: None, pinned_model: None, pinned_runtime: None,
            pinned_dtype: None, conformance_limit_micro: None, conformance_ref: None,
            settlement_denom: None, stake_denom: None,
            min_stake: None, verifier_fee_bps: Some(10_001), forfeit: None,
        }).unwrap_err();
    assert!(matches!(err, ContractError::BadFeeBps { bps: 10_001 }));
}

#[test]
fn zabanennyy_zalog_ne_zaberyot() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_attest(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    submit_checks(deps.as_mut(), id, &[FRAUD_MIN; 4]);
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
    // Зарегистрирован, допущен, залог внесён — всё, кроме пробы.
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::RegisterExecutor {
            model: MODEL.into(), runtime: RUNTIME.into(),
            dtype: DTYPE.into(), card_class: "rtx4090".into(),
        }).unwrap();
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &coins(MIN_STAKE, STAKE)),
        ExecuteMsg::DepositStake {}).unwrap();
    execute(deps.as_mut(), mock_env(), mock_info(ATTESTOR, &[]),
        ExecuteMsg::Attest { executor: EXEC.into(), eligible: true }).unwrap();
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
            dtype: DTYPE.into(), card_class: "rtx4090".into(),
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
fn chuzhaya_karta_probu_prohodit() {
    // Требовать одинаковых карт было бы неверно: замерено, что честный
    // узел на A100 и на 3090 расходится с 4090 максимум на 20 137 —
    // ниже предела 25 000. Проба отсеивает режим исполнения, а не карту.
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::RegisterExecutor {
            model: MODEL.into(), runtime: RUNTIME.into(),
            dtype: DTYPE.into(), card_class: "a100".into(),
        }).unwrap();
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::SubmitConformance {
            divergence_micro: 20_137,
            logprobs_hash: commit_hash(&probe_honest()),
        }).unwrap();
    let e: Executor = from_json(query(deps.as_ref(), mock_env(),
        QueryMsg::Executor { address: EXEC.into() }).unwrap()).unwrap();
    assert_eq!(e.conformance.unwrap().divergence_micro, 20_137);
}

#[test]
fn smena_svyazki_obnulyaet_probu() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_attest(deps.as_mut(), EXEC);
    submit_job(deps.as_mut());
    // Порог измерен для связки; сменили связку — все пробы о прежней.
    execute(deps.as_mut(), mock_env(), mock_info(ADMIN, &[]),
        ExecuteMsg::UpdateConfig {
            attestor: None, threshold_micro: None, checks_required: None,
            min_answer_tokens: None, pinned_model: None,
            pinned_runtime: Some("vllm-0.32.0".into()), pinned_dtype: None,
            conformance_limit_micro: None, conformance_ref: None,
            settlement_denom: None, stake_denom: None, min_stake: None,
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
    register_and_attest(deps.as_mut(), EXEC);
    submit_job(deps.as_mut());
    execute(deps.as_mut(), mock_env(), mock_info(ADMIN, &[]),
        ExecuteMsg::UpdateConfig {
            attestor: None, threshold_micro: None, checks_required: None,
            min_answer_tokens: None, pinned_model: None, pinned_runtime: None,
            pinned_dtype: None, conformance_limit_micro: None,
            conformance_ref: Some(ConformanceRef {
                ref_hash: commit_hash(&probe_honest()),
                lens: PROBE_LENS.to_vec(),
            }),
            settlement_denom: None, stake_denom: None, min_stake: None,
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
    register_and_attest(deps.as_mut(), EXEC);
    submit_job(deps.as_mut());
    execute(deps.as_mut(), mock_env(), mock_info(ADMIN, &[]),
        ExecuteMsg::UpdateConfig {
            attestor: None, threshold_micro: None, checks_required: None,
            min_answer_tokens: None, pinned_model: Some(MODEL.into()),
            pinned_runtime: Some(RUNTIME.into()), pinned_dtype: Some(DTYPE.into()),
            conformance_limit_micro: Some(CONF_LIMIT),
            conformance_ref: Some(ConformanceRef {
                ref_hash: commit_hash(&probe_ref()),
                lens: PROBE_LENS.to_vec(),
            }),
            settlement_denom: None, stake_denom: None, min_stake: None,
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
            dtype: DTYPE.into(), card_class: "rtx4090".into(),
        }).unwrap();
    // Расхождение своих векторов с эталоном он считает сам, поэтому
    // назвать может любое. Обязательство векторами делает это проверяемым.
    let mine: Vec<MicroLog> = probe_ref().iter().map(|x| x - 100_000).collect();
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::SubmitConformance {
            divergence_micro: HONEST_MEAN,
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
fn proba_po_maksimumu_a_ne_po_sredney_skleyke() {
    // САМОЕ ВАЖНОЕ свойство правила. Неверный режим исполнения портит
    // РЕДКИЕ тексты: у eager 29 из 40 векторов совпали с собой побитово,
    // а на остальных расхождение доходило до 413 488. Среднее по склейке
    // такую порчу размывает — замерено, что зазор падает с 20x до 1,5x, —
    // и жулик проходил бы пробу.
    //
    // Здесь первый текст сходится с эталоном точно, второй расходится на
    // 48 000. Среднее по склейке 24 000 — НИЖЕ предела 25 000. Максимум
    // по текстам 48 000 — выше.
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::RegisterExecutor {
            model: MODEL.into(), runtime: RUNTIME.into(),
            dtype: DTYPE.into(), card_class: "rtx4090".into(),
        }).unwrap();
    let mut mine = probe_ref();
    for x in mine.iter_mut().skip(PROBE_LENS[0] as usize) {
        *x -= 48_000;
    }
    assert_eq!(divergence_micro(&probe_ref(), &mine).unwrap(), 24_000,
        "среднее по склейке ниже предела — именно это и опасно");
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::SubmitConformance {
            divergence_micro: 24_000,
            logprobs_hash: commit_hash(&mine),
        }).unwrap();
    let res = execute(deps.as_mut(), mock_env(), mock_info("anyone", &[]),
        ExecuteMsg::DisputeConformance {
            executor: EXEC.into(),
            reference_logprobs: probe_ref(),
            executor_logprobs: mine,
        }).unwrap();
    assert!(res.attributes.iter().any(|a| a.key == "actual_micro"
        && a.value == "48000"), "считается максимум по текстам");
    assert!(res.attributes.iter().any(|a| a.key == "lied" && a.value == "true"));
}

#[test]
fn chestnuyu_probu_oproverzhenie_podtverzhdaet() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_attest(deps.as_mut(), EXEC);
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
    assert_eq!(e.conformance.unwrap().divergence_micro, HONEST_MEAN);
}

#[test]
fn oproverzhenie_podsunutym_etalonom_ne_proydyot() {
    // Иначе обвинить честного можно было бы, подставив другой эталон.
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_attest(deps.as_mut(), EXEC);
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
    register_and_attest(deps.as_mut(), EXEC);
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
            dtype: DTYPE.into(), card_class: "rtx4090".into(),
        }).unwrap();
    let short: Vec<MicroLog> = probe_honest()[..5].to_vec();
    execute(deps.as_mut(), mock_env(), mock_info(EXEC, &[]),
        ExecuteMsg::SubmitConformance {
            divergence_micro: HONEST_MEAN,
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
            admin: ADMIN.into(), attestor: ATTESTOR.into(),
            threshold_micro: THRESHOLD, checks_required: 4,
            min_answer_tokens: 32, pinned_model: MODEL.into(),
            pinned_runtime: RUNTIME.into(), pinned_dtype: DTYPE.into(),
            conformance_limit_micro: CONF_LIMIT,
            conformance_ref_hash: commit_hash(&probe_ref()),
            conformance_lens: vec![],
            settlement_denom: SETTLEMENT.into(), stake_denom: STAKE.into(),
            settlement_cw20: CW20.into(), min_stake: Uint128::new(MIN_STAKE),
            verifier_fee_bps: FEE_BPS, forfeit: Forfeit::Burn,
        }).unwrap_err();
    assert!(matches!(err, ContractError::EmptyProbe));
}

#[test]
fn zabanennyy_probu_ne_sdast() {
    // Иначе пойманный на подлоге сдаёт пробу заново и возвращается.
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_attest(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    submit_checks(deps.as_mut(), id, &[FRAUD_MIN; 4]);
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
    register_and_attest(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    let err = execute(deps.as_mut(), mock_env(), mock_info("nobody", &[]),
        ExecuteMsg::SubmitCheck {
            job_id: id, divergence_micro: FRAUD_MIN, logprobs_hash: "h".into(),
        }).unwrap_err();
    assert!(matches!(err, ContractError::ExecutorUnknown));
}

#[test]
fn proveryayushchiy_so_staroy_proboy_ne_prinimaetsya() {
    let mut deps = mock_dependencies();
    setup(deps.as_mut());
    register_and_attest(deps.as_mut(), EXEC);
    let id = job_until_answered(deps.as_mut());
    register_and_probe(deps.as_mut(), "checker0");
    // Сменили эталон — проба проверяющего больше не о действующей настройке.
    execute(deps.as_mut(), mock_env(), mock_info(ADMIN, &[]),
        ExecuteMsg::UpdateConfig {
            attestor: None, threshold_micro: None, checks_required: None,
            min_answer_tokens: None, pinned_model: None, pinned_runtime: None,
            pinned_dtype: None, conformance_limit_micro: None,
            conformance_ref: Some(ConformanceRef {
                ref_hash: commit_hash(&probe_honest()),
                lens: PROBE_LENS.to_vec(),
            }),
            settlement_denom: None, stake_denom: None, min_stake: None,
            verifier_fee_bps: None, forfeit: None,
        }).unwrap();
    let err = execute(deps.as_mut(), mock_env(), mock_info("checker0", &[]),
        ExecuteMsg::SubmitCheck {
            job_id: id, divergence_micro: HONEST_MEAN, logprobs_hash: "h".into(),
        }).unwrap_err();
    assert!(matches!(err, ContractError::ConformanceStale { have: 1, want: 2 }));
}
