"""Regras da dica do Bobby "lá fora × dentro" (UX item 11b): limites, dado ausente, portões e prioridade.

R1, R2 e R7 comparam pelo ponto de orvalho: a umidade de fora entra como a que esse ar teria na
temperatura de dentro. `rh_out_for` monta a umidade de fora que dá um equivalente exato, para os limites.
"""
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace

from app.schemas.grow import EnvironmentMeasurement
from app.services import bob_weather as bw

OFFSET = -3 * 3600  # America/Sao_Paulo
ON_AT, LIGHT_MINUTES = time(6, 0), 17 * 60 + 45  # 06:00 → 23:45


def ctx(local=time(12, 30), on_at=ON_AT, minutes=LIGHT_MINUTES, **values):
    """Linha de base em que nenhuma regra dispara; cada teste muda só o que a regra usa."""
    base = dict(grow_name='Sala', outdoor_temp_c=24.0, outdoor_humidity_rh=50.0, outdoor_temp_max_c=28.0,
                outdoor_temp_min_c=15.0, indoor_temp_c=24.0, indoor_humidity_rh=55.0, target_temp_c=None,
                target_humidity_rh=None, phase='veg')
    base.update(values)
    light = bw.light_state(on_at, minutes, datetime(2026, 10, 27, local.hour, local.minute))
    return bw.OutdoorIndoor(light=light, **base)


def rh_out_for(equivalent, out_t, in_t):
    """Umidade de fora (a out_t) que, levada a in_t, dá exatamente `equivalent` %."""
    return equivalent * bw._vapor_pressure_hpa(in_t) / bw._vapor_pressure_hpa(out_t)


class HelperTests(unittest.TestCase):
    def test_baseline_triggers_nothing(self):
        self.assertEqual([r.__name__ for r in bw.WEATHER_TIP_RULES if r(ctx())], [])

    def test_dew_point_magnus(self):
        self.assertAlmostEqual(bw.dew_point_c(25, 60), 16.7, places=1)
        self.assertAlmostEqual(bw.dew_point_c(22, 70), 16.3, places=1)
        self.assertIsNone(bw.dew_point_c(22, 0))

    def test_outdoor_humidity_at_indoor_temperature(self):
        self.assertAlmostEqual(bw.outdoor_rh_at_indoor_temp(24, 50, 24), 50.0, places=6)
        self.assertAlmostEqual(bw.outdoor_rh_at_indoor_temp(15, 75, 25), 40.4, places=1)  # frio e úmido → seco dentro
        self.assertAlmostEqual(bw.outdoor_rh_at_indoor_temp(35, 40, 25), 71.0, delta=0.1)  # quente e "seco" → úmido
        out = rh_out_for(75, 26, 25)
        self.assertAlmostEqual(bw.outdoor_rh_at_indoor_temp(26, out, 25), 75, places=6)

    def test_light_state(self):
        day = lambda h, m=0: datetime(2026, 10, 27, h, m)  # noqa: E731
        self.assertEqual(bw.light_state(ON_AT, LIGHT_MINUTES, day(12, 30)), bw.Light(True, time(23, 45), 675))
        self.assertEqual(bw.light_state(ON_AT, LIGHT_MINUTES, day(1)), bw.Light(False, time(23, 45), None))
        # atravessa a meia-noite: liga 18:00, 18 h de luz, apaga 12:00
        self.assertEqual(bw.light_state(time(18, 0), 18 * 60, day(2)), bw.Light(True, time(12, 0), 600))
        self.assertEqual(bw.light_state(None, 1440, day(2)), bw.Light(True, None, None))  # 24/0
        self.assertIsNone(bw.light_state(None, 720, day(2)))
        self.assertIsNone(bw.light_state(ON_AT, None, day(2)))
        self.assertIsNone(bw.light_state(ON_AT, LIGHT_MINUTES, None))  # sem fuso do local

    def test_most_advanced_phase_ignores_harvest_and_unknown(self):
        plants = [('a', 'veg'), ('b', 'flower'), ('c', 'harvest'), ('d', 'unknown'), ('e', 'seedling')]
        self.assertEqual(bw.most_advanced_phase(plants), ('flower', 'b'))
        self.assertEqual(bw.most_advanced_phase([('c', 'drying')]), (None, None))

    def test_texts_never_use_a_gendered_article_before_the_grow_name(self):
        cases = [
            ctx(local=time(19, 0), indoor_temp_c=22.0, indoor_humidity_rh=70.0, outdoor_temp_min_c=16.0),
            ctx(outdoor_temp_c=26.0, outdoor_humidity_rh=85.0, indoor_temp_c=25.0, indoor_humidity_rh=60.0),
            ctx(outdoor_temp_c=15.0, outdoor_humidity_rh=50.0, indoor_temp_c=25.0, indoor_humidity_rh=65.0),
            ctx(outdoor_temp_c=20.0, outdoor_humidity_rh=35.0, indoor_temp_c=25.0, indoor_humidity_rh=50.0,
                phase='seedling'),
            ctx(outdoor_temp_c=30.0, indoor_temp_c=25.0),
            ctx(outdoor_temp_c=15.0, local=time(2, 0)),
            ctx(outdoor_temp_c=24.0, outdoor_humidity_rh=55.0, target_temp_c=24.0, target_humidity_rh=55.0),
        ]
        fired = {}
        for case in cases:
            for rule in bw.WEATHER_TIP_RULES:
                tip = rule(case)
                if tip:
                    fired[tip.rule_id] = tip
        self.assertEqual(sorted(fired), ['R1', 'R2', 'R3', 'R4', 'R5', 'R6', 'R7'])
        for tip in fired.values():
            for text in (tip.title, tip.body, tip.cta_prompt):
                self.assertNotRegex(text, r'\b(da|na|a|o|do|no) Sala\b', tip.rule_id)


class R5CondensationTests(unittest.TestCase):
    # 22 °C e 70 % dentro: ponto de orvalho 16,3 °C
    def night(self, **values):
        return bw.r5_night_condensation(ctx(**{'local': time(19, 0), 'indoor_temp_c': 22.0,
                                               'indoor_humidity_rh': 70.0, 'outdoor_temp_min_c': 16.5, **values}))

    def test_fires_within_6h_of_lights_off(self):
        tip = self.night()
        self.assertEqual((tip.rule_id, tip.severity, tip.title), ('R5', 'attention', 'Risco de condensação nesta noite'))
        self.assertEqual(tip.body, 'A mínima de hoje lá fora é 16,5 °C, perto do ponto de orvalho em Sala (16,3 °C). '
                                   'Superfícies frias podem molhar quando a luz apagar às 23:45.')
        self.assertIn('com a luz apagada', self.night(local=time(1, 0)).body)

    def test_below_or_near_the_dew_point(self):
        self.assertIn('é 12 °C, abaixo do ponto de orvalho em Sala (16,3 °C)', self.night(outdoor_temp_min_c=12.0).body)
        self.assertIn('perto do ponto de orvalho', self.night(outdoor_temp_min_c=16.3).body)   # igual no texto
        self.assertIn('abaixo do ponto de orvalho', self.night(outdoor_temp_min_c=16.2).body)

    def test_limits(self):
        self.assertIsNone(self.night(indoor_humidity_rh=64.9))                      # < 65
        # floração: 60 % (Td 13,9 °C, por isso a mínima mais baixa)
        self.assertIsNotNone(self.night(indoor_humidity_rh=60.0, phase='flower', outdoor_temp_min_c=14.0))
        self.assertIsNone(self.night(indoor_humidity_rh=59.9, phase='late_flower'))
        self.assertIsNotNone(self.night(outdoor_temp_min_c=17.2))                    # Td 16,3 ≥ 16,2
        self.assertIsNone(self.night(outdoor_temp_min_c=17.4))                       # Td 16,3 < 16,4
        self.assertIsNotNone(self.night(local=time(17, 45)))                         # faltam 6 h
        self.assertIsNone(self.night(local=time(17, 44)))                            # faltam 6 h 01

    def test_missing_data(self):
        self.assertIsNone(self.night(outdoor_temp_min_c=None))
        self.assertIsNone(self.night(indoor_temp_c=None))
        self.assertIsNone(self.night(minutes=None))                # sem programação de luz
        self.assertIsNone(self.night(on_at=None, minutes=1440))    # 24/0 nunca apaga


class R2MoreHumidOutsideTests(unittest.TestCase):
    def run_rule(self, equivalent=None, out_t=26.0, in_t=25.0, **values):
        if equivalent is not None:
            values['outdoor_humidity_rh'] = rh_out_for(equivalent, out_t, in_t)
        return bw.r2_more_humid_outside(ctx(outdoor_temp_c=out_t, indoor_temp_c=in_t, **values))

    def test_fires_with_text(self):
        tip = self.run_rule(outdoor_humidity_rh=85.0, indoor_humidity_rh=60.0)
        self.assertEqual((tip.rule_id, tip.title), ('R2', 'Lá fora está mais úmido que em Sala'))
        self.assertEqual(tip.body, 'Fora: 85 % a 26 °C, o que dá cerca de 90 % na temperatura de dentro (25 °C). '
                                   'Dentro: 60 %. Puxar esse ar agora pode subir a umidade dentro. '
                                   'Acompanhe a leitura na próxima hora.')

    def test_limits_on_the_equivalent_humidity(self):
        self.assertIsNotNone(self.run_rule(75.01, indoor_humidity_rh=60.0))
        self.assertIsNone(self.run_rule(74.99, indoor_humidity_rh=59.0))     # abaixo de 75 %
        self.assertIsNone(self.run_rule(80.99, indoor_humidity_rh=66.0))     # diferença < 15
        self.assertIsNotNone(self.run_rule(81.01, indoor_humidity_rh=66.0))

    def test_flowering_lowers_limit_to_70(self):
        tip = self.run_rule(70.01, indoor_humidity_rh=55.0, phase='flower')
        self.assertIn('Na floração, puxar esse ar', tip.body)
        self.assertIn('phase', tip.uses)
        self.assertIsNone(self.run_rule(70.01, indoor_humidity_rh=55.0))      # vegetativa
        self.assertIsNone(self.run_rule(69.99, indoor_humidity_rh=50.0, phase='flower'))

    def test_cold_humid_air_does_not_raise_humidity(self):
        # 15 °C e 75 % lá fora é ~40 % a 25 °C: a regra antiga (UR crua) dispararia, a física não
        self.assertTrue(75 >= 50 + 15)
        self.assertIsNone(self.run_rule(out_t=15.0, outdoor_humidity_rh=75.0, indoor_humidity_rh=50.0))

    def test_missing_indoor_data(self):
        self.assertIsNone(self.run_rule(95, indoor_humidity_rh=None))
        self.assertIsNone(self.run_rule(outdoor_humidity_rh=95.0, in_t=None, indoor_humidity_rh=40.0))


class R1DrierOutsideTests(unittest.TestCase):
    def run_rule(self, equivalent=None, out_t=15.0, in_t=25.0, **values):
        if equivalent is not None:
            values['outdoor_humidity_rh'] = rh_out_for(equivalent, out_t, in_t)
        return bw.r1_drier_outside(ctx(outdoor_temp_c=out_t, indoor_temp_c=in_t, **values))

    def test_with_target(self):
        tip = self.run_rule(outdoor_humidity_rh=50.0, indoor_humidity_rh=65.0, target_humidity_rh=55.0)
        self.assertEqual((tip.rule_id, tip.title), ('R1', 'Lá fora está bem mais seco'))
        self.assertEqual(tip.body, 'Fora: 50 % a 15 °C, o que dá cerca de 27 % na temperatura de dentro (25 °C). '
                                   'Em Sala: 65 %, acima da sua faixa de 55 %. '
                                   'Renovar o ar pela exaustão tende a baixar a umidade dentro.')
        self.assertIsNotNone(self.run_rule(39.99, indoor_humidity_rh=60.0, target_humidity_rh=55.0))
        self.assertIsNone(self.run_rule(40.01, indoor_humidity_rh=60.0, target_humidity_rh=55.0))  # diferença < 20
        self.assertIsNone(self.run_rule(30, indoor_humidity_rh=58.0, target_humidity_rh=55.0))      # não passa de 58

    def test_without_target_uses_60_or_55_in_flower(self):
        self.assertIsNotNone(self.run_rule(30, indoor_humidity_rh=61.0))
        self.assertIsNone(self.run_rule(30, indoor_humidity_rh=60.0))
        tip = self.run_rule(30, indoor_humidity_rh=56.0, phase='flower')
        self.assertNotIn('faixa', tip.body)
        self.assertIsNone(self.run_rule(30, indoor_humidity_rh=55.0, phase='flower'))

    def test_hot_air_with_low_relative_humidity_is_not_drier(self):
        # 35 °C e 40 % lá fora é ~71 % a 25 °C: tem mais água que os 60 % de dentro
        self.assertTrue(40 <= 60 - 20)
        self.assertIsNone(self.run_rule(out_t=35.0, outdoor_humidity_rh=40.0, indoor_humidity_rh=60.0))

    def test_missing_indoor_data(self):
        self.assertIsNone(self.run_rule(10, indoor_humidity_rh=None))
        self.assertIsNone(self.run_rule(outdoor_humidity_rh=10.0, in_t=None, indoor_humidity_rh=70.0))


class R7DryAirForSeedlingsTests(unittest.TestCase):
    def run_rule(self, equivalent=None, out_t=20.0, in_t=25.0, **values):
        if equivalent is not None:
            values['outdoor_humidity_rh'] = rh_out_for(equivalent, out_t, in_t)
        return bw.r7_dry_air_for_seedlings(ctx(**{'phase': 'seedling', 'outdoor_temp_c': out_t,
                                                  'indoor_temp_c': in_t, **values}))

    def test_fires_with_text(self):
        tip = self.run_rule(outdoor_humidity_rh=35.0, indoor_humidity_rh=50.0)
        self.assertEqual(tip.body, 'Fora: 35 % a 20 °C, o que dá cerca de 26 % na temperatura de dentro (25 °C). '
                                   'Mudas costumam pedir umidade mais alta; trazer esse ar pode secar ainda mais '
                                   'o ar em Sala (50 %).')
        self.assertIsNotNone(self.run_rule(30, indoor_humidity_rh=50.0, phase='germination'))

    def test_limits(self):
        self.assertIsNotNone(self.run_rule(39.99, indoor_humidity_rh=50.0))
        self.assertIsNone(self.run_rule(40.01, indoor_humidity_rh=50.0))      # não está seco a 25 °C
        self.assertIsNone(self.run_rule(30, indoor_humidity_rh=65.0))         # dentro já úmido
        self.assertIsNone(self.run_rule(30, indoor_humidity_rh=60.0, target_humidity_rh=60.0))
        self.assertIsNotNone(self.run_rule(30, indoor_humidity_rh=59.0, target_humidity_rh=60.0))
        self.assertIsNone(self.run_rule(35, indoor_humidity_rh=34.0))         # não seca mais que o ar de dentro

    def test_hot_air_with_low_relative_humidity_is_not_dry(self):
        # 35 °C e 35 % lá fora é ~62 % a 25 °C
        self.assertIsNone(self.run_rule(out_t=35.0, outdoor_humidity_rh=35.0, indoor_humidity_rh=50.0))

    def test_only_young_plants_and_needs_reading(self):
        self.assertIsNone(self.run_rule(30, indoor_humidity_rh=50.0, phase='veg'))
        self.assertIsNone(self.run_rule(30, indoor_humidity_rh=50.0, phase=None))
        self.assertIsNone(self.run_rule(30, indoor_humidity_rh=None))
        self.assertIsNone(self.run_rule(outdoor_humidity_rh=20.0, in_t=None, indoor_humidity_rh=50.0))


class R3HotterOutsideTests(unittest.TestCase):
    def run_rule(self, **values):
        return bw.r3_hotter_outside(ctx(**values))

    def test_with_target(self):
        tip = self.run_rule(outdoor_temp_c=28.0, indoor_temp_c=23.0, target_temp_c=24.0)
        self.assertEqual((tip.rule_id, tip.title), ('R3', 'Lá fora está mais quente que a sua faixa'))
        self.assertEqual(tip.body, 'Fora: 28 °C. Sua faixa: 24 °C. Dentro: 23 °C. Puxar ar de fora agora tende a '
                                   'aquecer o ar em Sala; a troca rende mais quando esfriar (mínima de hoje 15 °C).')
        self.assertIsNone(self.run_rule(outdoor_temp_c=27.9, indoor_temp_c=23.0, target_temp_c=24.0))
        self.assertIsNone(self.run_rule(outdoor_temp_c=28.0, indoor_temp_c=22.9, target_temp_c=24.0))

    def test_outside_must_be_hotter_than_inside(self):
        self.assertIsNone(self.run_rule(outdoor_temp_c=28.0, indoor_temp_c=31.0, target_temp_c=24.0))
        self.assertIsNone(self.run_rule(outdoor_temp_c=28.0, indoor_temp_c=28.0, target_temp_c=24.0))
        self.assertIsNotNone(self.run_rule(outdoor_temp_c=28.1, indoor_temp_c=28.0, target_temp_c=24.0))

    def test_without_target_and_without_minimum(self):
        tip = self.run_rule(outdoor_temp_c=30.0, indoor_temp_c=25.0, outdoor_temp_min_c=None)
        self.assertEqual(tip.title, 'Lá fora está quente')
        self.assertTrue(tip.body.endswith('a troca rende mais quando esfriar.'))
        self.assertIsNone(self.run_rule(outdoor_temp_c=29.9, indoor_temp_c=25.0))
        self.assertIsNone(self.run_rule(outdoor_temp_c=35.0, indoor_temp_c=None))


class R4ColdNightTests(unittest.TestCase):
    def run_rule(self, **values):
        return bw.r4_cold_night_outside(ctx(**values))

    def test_with_target_and_light_about_to_go_off(self):
        tip = self.run_rule(outdoor_temp_c=17.0, target_temp_c=25.0, local=time(21, 0))  # faltam 2 h 45
        self.assertEqual(tip.body, 'Fora: 17 °C. Sua faixa: 25 °C. Com a luz apagada, a troca de ar pode deixar '
                                   'o ar em Sala abaixo da faixa.')
        self.assertIsNotNone(self.run_rule(outdoor_temp_c=17.0, target_temp_c=25.0, local=time(2, 0)))  # apagada
        self.assertIsNone(self.run_rule(outdoor_temp_c=17.1, target_temp_c=25.0, local=time(21, 0)))
        self.assertIsNone(self.run_rule(outdoor_temp_c=17.0, target_temp_c=25.0, local=time(20, 44)))  # 3 h 01

    def test_without_target_and_without_schedule(self):
        self.assertEqual(self.run_rule(outdoor_temp_c=15.0, local=time(2, 0)).title, 'A noite lá fora está fria')
        self.assertIsNone(self.run_rule(outdoor_temp_c=15.1, local=time(2, 0)))
        self.assertIsNone(self.run_rule(outdoor_temp_c=5.0, minutes=None))
        self.assertIsNone(self.run_rule(outdoor_temp_c=5.0, on_at=None, minutes=1440))


class R6GoodTimeTests(unittest.TestCase):
    def run_rule(self, **values):
        return bw.r6_good_time_to_exchange(ctx(**{'target_temp_c': 24.0, 'target_humidity_rh': 55.0, **values}))

    def test_fires_and_limits(self):
        tip = self.run_rule(outdoor_temp_c=26.0, outdoor_humidity_rh=65.0)
        self.assertEqual(tip.body, 'Lá fora está 26 °C e 65 %, dentro da sua faixa. Trocar o ar agora mexe pouco '
                                   'na temperatura e na umidade em Sala.')
        self.assertIsNone(self.run_rule(outdoor_temp_c=26.1, outdoor_humidity_rh=65.0))
        self.assertIsNone(self.run_rule(outdoor_temp_c=24.0, outdoor_humidity_rh=44.0))

    def test_needs_both_targets(self):
        self.assertIsNone(self.run_rule(target_humidity_rh=None))
        self.assertIsNone(self.run_rule(target_temp_c=None))


class WeatherTipTests(unittest.TestCase):
    """weather_tip: portões, prioridade e fontes. Lá fora 24 °C e 30 %; dentro 22 °C e 70 %; 19:00 local."""

    def setUp(self):
        self.grow_id = uuid.uuid4()
        self.reading_id = uuid.uuid4()
        self.now = datetime(2026, 10, 27, 22, 0, tzinfo=timezone.utc)  # 19:00 em São Paulo
        self.grow = SimpleNamespace(id=self.grow_id, name='Sala', grow_type='indoor', target_temp_c=None,
                                    target_humidity_rh=None, photoperiod_on_at=ON_AT,
                                    photoperiod_light_minutes=LIGHT_MINUTES)
        self.weather = SimpleNamespace(temperature=24.0, humidity=30, temp_max=28.0, temp_min=16.0,
                                       fetched_at=self.now - timedelta(minutes=5), utc_offset_seconds=OFFSET)

    def indoor(self, temp=22.0, humidity=70.0, age=timedelta(minutes=13), humidity_age=None):
        def m(value, unit, at):
            return EnvironmentMeasurement(value=value, unit=unit, source='manual', source_id=self.reading_id,
                                          source_kind='environment_reading', observed_at=self.now - at)
        return {'temperature': m(temp, '°C', age), 'humidity': m(humidity, '%', humidity_age or age),
                'light': EnvironmentMeasurement(unit='lux'), 'vpd': EnvironmentMeasurement(unit='kPa')}

    def tip(self, indoor=None, phase='veg', plant='p1'):
        return bw.weather_tip(grow=self.grow, weather=self.weather, indoor=indoor or self.indoor(), phase=phase,
                              phase_plant_id=plant, now=self.now,
                              weather_src=bw.weather_source(lat=-22.91, lon=-47.06, label='Campinas, SP',
                                                            fetched_at=self.weather.fetched_at,
                                                            grow_id=str(self.grow_id)))

    def test_priority_r5_beats_r1(self):
        self.assertIsNotNone(bw.r1_drier_outside(ctx(local=time(19, 0), outdoor_humidity_rh=30.0, indoor_temp_c=22.0,
                                                     indoor_humidity_rh=70.0, outdoor_temp_min_c=16.0)))
        tip = self.tip()
        self.assertEqual(tip['rule_id'], 'R5')
        self.assertEqual([s['kind'] for s in tip['sources']], ['weather', 'environment_reading', 'light_schedule'])
        self.assertEqual(tip['sources'][1]['title'], 'Registro do ambiente · 22 °C · 70%')  # um registro, dois valores
        self.assertEqual(tip['sources'][2]['title'], 'Programação de luz · 06:00 → 23:45')
        self.assertEqual(tip['cta_prompt'], 'Risco de condensação nesta noite. Como usar isso na exaustão de Sala hoje?')

    def test_lower_priority_wins_only_when_higher_does_not_fire(self):
        self.weather.temp_min = 25.0  # mínima bem acima do ponto de orvalho: sem R5, fica a R1
        tip = self.tip()
        self.assertEqual((tip['rule_id'], tip['severity']), ('R1', 'info'))
        self.assertEqual([s['kind'] for s in tip['sources']], ['weather', 'environment_reading'])
        self.assertEqual(tip['sources'][1]['title'], 'Registro do ambiente · 22 °C · 70%')
        self.grow.target_humidity_rh = 60.0
        self.assertEqual(self.tip()['sources'][-1]['title'], 'Faixa definida · 60%')

    def test_r6_only_when_nothing_above_fires(self):
        self.weather.temp_min, self.weather.humidity = 25.0, 50
        self.grow.target_temp_c, self.grow.target_humidity_rh = 24.0, 55.0
        self.assertEqual(self.tip(self.indoor(temp=24.0, humidity=55.0))['rule_id'], 'R6')
        self.assertEqual(self.tip(self.indoor(temp=24.0, humidity=72.0))['rule_id'], 'R1')

    def test_phase_source_points_to_the_plant(self):
        self.weather.temperature, self.weather.humidity = 26.0, 72  # 72 % também a 26 °C dentro
        self.assertIsNone(self.tip(self.indoor(temp=26.0, humidity=50.0), phase='veg'))
        tip = self.tip(self.indoor(temp=26.0, humidity=50.0), phase='flower', plant='p9')
        self.assertEqual(tip['rule_id'], 'R2')
        self.assertEqual(tip['sources'][-1], {'kind': 'plant', 'id': 'p9', 'title': 'Fase · Floração', 'date': None,
                                              'scope': 'focus', 'plant_id': 'p9', 'grow_id': str(self.grow_id)})

    def test_gates(self):
        self.assertIsNotNone(self.tip())
        self.grow.grow_type = 'outdoor'
        self.assertIsNone(self.tip())
        self.grow.grow_type = 'indoor'
        self.assertIsNone(self.tip(self.indoor(age=timedelta(hours=6, minutes=1))))        # leitura antiga (R8)
        self.assertIsNotNone(self.tip(self.indoor(age=timedelta(hours=6))))
        self.weather.fetched_at = self.now - timedelta(hours=1, minutes=1)                   # clima antigo
        self.assertIsNone(self.tip())

    def test_stale_metric_counts_as_missing(self):
        self.weather.temp_min = 25.0
        # umidade com 7 h: R1 (que usa a umidade) não pode disparar com o dado velho
        self.assertIsNone(self.tip(self.indoor(humidity_age=timedelta(hours=7))))

    def test_without_timezone_rules_that_need_the_clock_do_not_fire(self):
        self.weather.utc_offset_seconds = None
        self.assertEqual(self.tip()['rule_id'], 'R1')  # R5 depende da hora local da luz

    def test_ctx_is_immutable(self):
        with self.assertRaises(Exception):
            ctx().phase = 'flower'  # noqa
        self.assertEqual(replace(ctx(), phase='flower').flowering, True)


if __name__ == '__main__':
    unittest.main()
